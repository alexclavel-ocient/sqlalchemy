# mysql/reflection.py
# Copyright (C) 2005-2020 the SQLAlchemy authors and contributors
# <see AUTHORS file>
#
# This module is part of SQLAlchemy and is released under
# the MIT License: http://www.opensource.org/licenses/mit-license.php

import re

from .enumerated import ENUM
from .enumerated import SET
from .types import DATETIME
from .types import TIME
from .types import TIMESTAMP
from ... import log
from ... import types as sqltypes
from ... import util


class ReflectedState(object):
    """Stores raw information about a SHOW CREATE TABLE statement."""

    def __init__(self):
        self.columns = []
        self.table_options = {}
        self.table_name = None
        self.keys = []
        self.fk_constraints = []
        self.ck_constraints = []


@log.class_logger
class MySQLTableDefinitionParser(object):
    """Parses the results of a SHOW CREATE TABLE statement."""

    def __init__(self, dialect, preparer):
        self.dialect = dialect
        self.preparer = preparer
        self._prep_regexes()

    def parse(self, show_create, charset):
        state = ReflectedState()
        state.charset = charset
        for line in re.split(r"\r?\n", show_create):
            if line.startswith("  " + self.preparer.initial_quote):
                self._parse_column(line, state)
            # a regular table options line
            elif line.startswith(") "):
                self._parse_table_options(line, state)
            # an ANSI-mode table options line
            elif line == ")":
                pass
            elif line.startswith("CREATE "):
                self._parse_table_name(line, state)
            # Not present in real reflection, but may be if
            # loading from a file.
            elif not line:
                pass
            else:
                type_, spec = self._parse_constraints(line)
                if type_ is None:
                    util.warn("Unknown schema content: %r" % line)
                elif type_ == "key":
                    state.keys.append(spec)
                elif type_ == "fk_constraint":
                    state.fk_constraints.append(spec)
                elif type_ == "ck_constraint":
                    state.ck_constraints.append(spec)
                else:
                    pass
        return state

    def _parse_constraints(self, line):
        """Parse a KEY or CONSTRAINT line.

        :param line: A line of SHOW CREATE TABLE output
        """

        # KEY
        m = self._re_key.match(line)
        if m:
            spec = m.groupdict()
            # convert columns into name, length pairs
            # NOTE: we may want to consider SHOW INDEX as the
            # format of indexes in MySQL becomes more complex
            spec["columns"] = self._parse_keyexprs(spec["columns"])
            if spec["version_sql"]:
                m2 = self._re_key_version_sql.match(spec["version_sql"])
                if m2 and m2.groupdict()["parser"]:
                    spec["parser"] = m2.groupdict()["parser"]
            if spec["parser"]:
                spec["parser"] = self.preparer.unformat_identifiers(
                    spec["parser"]
                )[0]
            return "key", spec

        # FOREIGN KEY CONSTRAINT
        m = self._re_fk_constraint.match(line)
        if m:
            spec = m.groupdict()
            spec["table"] = self.preparer.unformat_identifiers(spec["table"])
            spec["local"] = [c[0] for c in self._parse_keyexprs(spec["local"])]
            spec["foreign"] = [
                c[0] for c in self._parse_keyexprs(spec["foreign"])
            ]
            return "fk_constraint", spec

        # CHECK constraint
        m = self._re_ck_constraint.match(line)
        if m:
            spec = m.groupdict()
            return "ck_constraint", spec

        # PARTITION and SUBPARTITION
        m = self._re_partition.match(line)
        if m:
            # Punt!
            return "partition", line

        # No match.
        return (None, line)

    def _parse_table_name(self, line, state):
        """Extract the table name.

        :param line: The first line of SHOW CREATE TABLE
        """

        regex, cleanup = self._pr_name
        m = regex.match(line)
        if m:
            state.table_name = cleanup(m.group("name"))

    def _parse_table_options(self, line, state):
        """Build a dictionary of all reflected table-level options.

        :param line: The final line of SHOW CREATE TABLE output.
        """

        options = {}

        if not line or line == ")":
            pass

        else:
            rest_of_line = line[:]
            for regex, cleanup in self._pr_options:
                m = regex.search(rest_of_line)
                if not m:
                    continue
                directive, value = m.group("directive"), m.group("val")
                if cleanup:
                    value = cleanup(value)
                options[directive.lower()] = value
                rest_of_line = regex.sub("", rest_of_line)

        for nope in ("auto_increment", "data directory", "index directory"):
            options.pop(nope, None)

        for opt, val in options.items():
            state.table_options["%s_%s" % (self.dialect.name, opt)] = val

    def _parse_column(self, line, state):
        """Extract column details.

        Falls back to a 'minimal support' variant if full parse fails.

        :param line: Any column-bearing line from SHOW CREATE TABLE
        """

        spec = None
        m = self._re_column.match(line)
        if m:
            spec = m.groupdict()
            spec["full"] = True
        else:
            m = self._re_column_loose.match(line)
            if m:
                spec = m.groupdict()
                spec["full"] = False
        if not spec:
            util.warn("Unknown column definition %r" % line)
            return
        if not spec["full"]:
            util.warn("Incomplete reflection of column definition %r" % line)

        name, type_, args = spec["name"], spec["coltype"], spec["arg"]

        try:
            col_type = self.dialect.ischema_names[type_]
        except KeyError:
            util.warn(
                "Did not recognize type '%s' of column '%s'" % (type_, name)
            )
            col_type = sqltypes.NullType

        # Column type positional arguments eg. varchar(32)
        if args is None or args == "":
            type_args = []
        elif args[0] == "'" and args[-1] == "'":
            type_args = self._re_csv_str.findall(args)
        else:
            type_args = [int(v) for v in self._re_csv_int.findall(args)]

        # Column type keyword options
        type_kw = {}

        if issubclass(col_type, (DATETIME, TIME, TIMESTAMP)):
            if type_args:
                type_kw["fsp"] = type_args.pop(0)

        for kw in ("unsigned", "zerofill"):
            if spec.get(kw, False):
                type_kw[kw] = True
        for kw in ("charset", "collate"):
            if spec.get(kw, False):
                type_kw[kw] = spec[kw]
        if issubclass(col_type, (ENUM, SET)):
            type_args = _strip_values(type_args)

            if issubclass(col_type, SET) and "" in type_args:
                type_kw["retrieve_as_bitwise"] = True

        type_instance = col_type(*type_args, **type_kw)

        col_kw = {}

        # NOT NULL
        col_kw["nullable"] = True
        # this can be "NULL" in the case of TIMESTAMP
        if spec.get("notnull", False) == "NOT NULL":
            col_kw["nullable"] = False

        # AUTO_INCREMENT
        if spec.get("autoincr", False):
            col_kw["autoincrement"] = True
        elif issubclass(col_type, sqltypes.Integer):
            col_kw["autoincrement"] = False

        # DEFAULT
        default = spec.get("default", None)

        if default == "NULL":
            # eliminates the need to deal with this later.
            default = None

        comment = spec.get("comment", None)

        if comment is not None:
            comment = comment.replace("\\\\", "\\").replace("''", "'")

        sqltext = spec.get("generated")
        if sqltext is not None:
            computed = dict(sqltext=sqltext)
            persisted = spec.get("persistence")
            if persisted is not None:
                computed["persisted"] = persisted == "STORED"
            col_kw["computed"] = computed

        col_d = dict(
            name=name, type=type_instance, default=default, comment=comment
        )
        col_d.update(col_kw)
        state.columns.append(col_d)

    def _describe_to_create(self, table_name, columns):
        """Re-format DESCRIBE output as a SHOW CREATE TABLE string.

        DESCRIBE is a much simpler reflection and is sufficient for
        reflecting views for runtime use.  This method formats DDL
        for columns only- keys are omitted.

        :param columns: A sequence of DESCRIBE or SHOW COLUMNS 6-tuples.
          SHOW FULL COLUMNS FROM rows must be rearranged for use with
          this function.
        """

        buffer = []
        for row in columns:
            (name, col_type, nullable, default, extra) = [
                row[i] for i in (0, 1, 2, 4, 5)
            ]

            line = [" "]
            line.append(self.preparer.quote_identifier(name))
            line.append(col_type)
            if not nullable:
                line.append("NOT NULL")
            if default:
                if "auto_increment" in default:
                    pass
                elif col_type.startswith("timestamp") and default.startswith(
                    "C"
                ):
                    line.append("DEFAULT")
                    line.append(default)
                elif default == "NULL":
                    line.append("DEFAULT")
                    line.append(default)
                else:
                    line.append("DEFAULT")
                    line.append("'%s'" % default.replace("'", "''"))
            if extra:
                line.append(extra)

            buffer.append(" ".join(line))

        return "".join(
            [
                (
                    "CREATE TABLE %s (\n"
                    % self.preparer.quote_identifier(table_name)
                ),
                ",\n".join(buffer),
                "\n) ",
            ]
        )

    def _parse_keyexprs(self, identifiers):
        """Unpack '"col"(2),"col" ASC'-ish strings into components."""

        return self._re_keyexprs.findall(identifiers)

    def _prep_regexes(self):
        """Pre-compile regular expressions."""

        self._re_columns = []
        self._pr_options = []

        _final = self.preparer.final_quote

        quotes = dict(
            zip(
                ("iq", "fq", "esc_fq"),
                [
                    re.escape(s)
                    for s in (
                        self.preparer.initial_quote,
                        _final,
                        self.preparer._escape_identifier(_final),
                    )
                ],
            )
        )

        self._pr_name = _pr_compile(
            r"^CREATE (?:\w+ +)?TABLE +"
            r"%(iq)s(?P<name>(?:%(esc_fq)s|[^%(fq)s])+)%(fq)s +\($" % quotes,
            self.preparer._unescape_identifier,
        )

        # `col`,`col2`(32),`col3`(15) DESC
        #
        self._re_keyexprs = _re_compile(
            r"(?:"
            r"(?:%(iq)s((?:%(esc_fq)s|[^%(fq)s])+)%(fq)s)"
            r"(?:\((\d+)\))?(?: +(ASC|DESC))?(?=\,|$))+" % quotes
        )

        # 'foo' or 'foo','bar' or 'fo,o','ba''a''r'
        self._re_csv_str = _re_compile(r"\x27(?:\x27\x27|[^\x27])*\x27")

        # 123 or 123,456
        self._re_csv_int = _re_compile(r"\d+")

        # `colname` <type> [type opts]
        #  (NOT NULL | NULL)
        #   DEFAULT ('value' | CURRENT_TIMESTAMP...)
        #   COMMENT 'comment'
        #  COLUMN_FORMAT (FIXED|DYNAMIC|DEFAULT)
        #  STORAGE (DISK|MEMORY)
        self._re_column = _re_compile(
            r"  "
            r"%(iq)s(?P<name>(?:%(esc_fq)s|[^%(fq)s])+)%(fq)s +"
            r"(?P<coltype>\w+)"
            r"(?:\((?P<arg>(?:\d+|\d+,\d+|"
            r"(?:'(?:''|[^'])*',?)+))\))?"
            r"(?: +(?P<unsigned>UNSIGNED))?"
            r"(?: +(?P<zerofill>ZEROFILL))?"
            r"(?: +CHARACTER SET +(?P<charset>[\w_]+))?"
            r"(?: +COLLATE +(?P<collate>[\w_]+))?"
            r"(?: +(?P<notnull>(?:NOT )?NULL))?"
            r"(?: +DEFAULT +(?P<default>"
            r"(?:NULL|'(?:''|[^'])*'|[\w\.\(\)]+"
            r"(?: +ON UPDATE [\w\.\(\)]+)?)"
            r"))?"
            r"(?: +(?:GENERATED ALWAYS)? ?AS +(?P<generated>\("
            r".*\))? ?(?P<persistence>VIRTUAL|STORED)?)?"
            r"(?: +(?P<autoincr>AUTO_INCREMENT))?"
            r"(?: +COMMENT +'(?P<comment>(?:''|[^'])*)')?"
            r"(?: +COLUMN_FORMAT +(?P<colfmt>\w+))?"
            r"(?: +STORAGE +(?P<storage>\w+))?"
            r"(?: +(?P<extra>.*))?"
            r",?$" % quotes
        )

        # Fallback, try to parse as little as possible
        self._re_column_loose = _re_compile(
            r"  "
            r"%(iq)s(?P<name>(?:%(esc_fq)s|[^%(fq)s])+)%(fq)s +"
            r"(?P<coltype>\w+)"
            r"(?:\((?P<arg>(?:\d+|\d+,\d+|\x27(?:\x27\x27|[^\x27])+\x27))\))?"
            r".*?(?P<notnull>(?:NOT )NULL)?" % quotes
        )

        # (PRIMARY|UNIQUE|FULLTEXT|SPATIAL) INDEX `name` (USING (BTREE|HASH))?
        # (`col` (ASC|DESC)?, `col` (ASC|DESC)?)
        # KEY_BLOCK_SIZE size | WITH PARSER name  /*!50100 WITH PARSER name */
        self._re_key = _re_compile(
            r"  "
            r"(?:(?P<type>\S+) )?KEY"
            r"(?: +%(iq)s(?P<name>(?:%(esc_fq)s|[^%(fq)s])+)%(fq)s)?"
            r"(?: +USING +(?P<using_pre>\S+))?"
            r" +\((?P<columns>.+?)\)"
            r"(?: +USING +(?P<using_post>\S+))?"
            r"(?: +KEY_BLOCK_SIZE *[ =]? *(?P<keyblock>\S+))?"
            r"(?: +WITH PARSER +(?P<parser>\S+))?"
            r"(?: +COMMENT +(?P<comment>(\x27\x27|\x27([^\x27])*?\x27)+))?"
            r"(?: +/\*(?P<version_sql>.+)\*/ +)?"
            r",?$" % quotes
        )

        # https://forums.mysql.com/read.php?20,567102,567111#msg-567111
        # It means if the MySQL version >= \d+, execute what's in the comment
        self._re_key_version_sql = _re_compile(
            r"\!\d+ " r"(?: *WITH PARSER +(?P<parser>\S+) *)?"
        )

        # CONSTRAINT `name` FOREIGN KEY (`local_col`)
        # REFERENCES `remote` (`remote_col`)
        # MATCH FULL | MATCH PARTIAL | MATCH SIMPLE
        # ON DELETE CASCADE ON UPDATE RESTRICT
        #
        # unique constraints come back as KEYs
        kw = quotes.copy()
        kw["on"] = "RESTRICT|CASCADE|SET NULL|NO ACTION"
        self._re_fk_constraint = _re_compile(
            r"  "
            r"CONSTRAINT +"
            r"%(iq)s(?P<name>(?:%(esc_fq)s|[^%(fq)s])+)%(fq)s +"
            r"FOREIGN KEY +"
            r"\((?P<local>[^\)]+?)\) REFERENCES +"
            r"(?P<table>%(iq)s[^%(fq)s]+%(fq)s"
            r"(?:\.%(iq)s[^%(fq)s]+%(fq)s)?) +"
            r"\((?P<foreign>[^\)]+?)\)"
            r"(?: +(?P<match>MATCH \w+))?"
            r"(?: +ON DELETE (?P<ondelete>%(on)s))?"
            r"(?: +ON UPDATE (?P<onupdate>%(on)s))?" % kw
        )

        # CONSTRAINT `CONSTRAINT_1` CHECK (`x` > 5)'
        # testing on MariaDB 10.2 shows that the CHECK constraint
        # is returned on a line by itself, so to match without worrying
        # about parenthesis in the expresion we go to the end of the line
        self._re_ck_constraint = _re_compile(
            r"  "
            r"CONSTRAINT +"
            r"%(iq)s(?P<name>(?:%(esc_fq)s|[^%(fq)s])+)%(fq)s +"
            r"CHECK +"
            r"\((?P<sqltext>.+)\),?" % kw
        )

        # PARTITION
        #
        # punt!
        self._re_partition = _re_compile(r"(?:.*)(?:SUB)?PARTITION(?:.*)")

        # Table-level options (COLLATE, ENGINE, etc.)
        # Do the string options first, since they have quoted
        # strings we need to get rid of.
        for option in _options_of_type_string:
            self._add_option_string(option)

        for option in (
            "ENGINE",
            "TYPE",
            "AUTO_INCREMENT",
            "AVG_ROW_LENGTH",
            "CHARACTER SET",
            "DEFAULT CHARSET",
            "CHECKSUM",
            "COLLATE",
            "DELAY_KEY_WRITE",
            "INSERT_METHOD",
            "MAX_ROWS",
            "MIN_ROWS",
            "PACK_KEYS",
            "ROW_FORMAT",
            "KEY_BLOCK_SIZE",
        ):
            self._add_option_word(option)

        self._add_option_regex("UNION", r"\([^\)]+\)")
        self._add_option_regex("TABLESPACE", r".*? STORAGE DISK")
        self._add_option_regex(
            "RAID_TYPE",
            r"\w+\s+RAID_CHUNKS\s*\=\s*\w+RAID_CHUNKSIZE\s*=\s*\w+",
        )

    _optional_equals = r"(?:\s*(?:=\s*)|\s+)"

    def _add_option_string(self, directive):
        regex = r"(?P<directive>%s)%s" r"'(?P<val>(?:[^']|'')*?)'(?!')" % (
            re.escape(directive),
            self._optional_equals,
        )
        self._pr_options.append(
            _pr_compile(
                regex, lambda v: v.replace("\\\\", "\\").replace("''", "'")
            )
        )

    def _add_option_word(self, directive):
        regex = r"(?P<directive>%s)%s" r"(?P<val>\w+)" % (
            re.escape(directive),
            self._optional_equals,
        )
        self._pr_options.append(_pr_compile(regex))

    def _add_option_regex(self, directive, regex):
        regex = r"(?P<directive>%s)%s" r"(?P<val>%s)" % (
            re.escape(directive),
            self._optional_equals,
            regex,
        )
        self._pr_options.append(_pr_compile(regex))


_options_of_type_string = (
    "COMMENT",
    "DATA DIRECTORY",
    "INDEX DIRECTORY",
    "PASSWORD",
    "CONNECTION",
)


def _pr_compile(regex, cleanup=None):
    """Prepare a 2-tuple of compiled regex and callable."""

    return (_re_compile(regex), cleanup)


def _re_compile(regex):
    """Compile a string to regex, I and UNICODE."""

    return re.compile(regex, re.I | re.UNICODE)


def _strip_values(values):
    "Strip reflected values quotes"
    strip_values = []
    for a in values:
        if a[0:1] == '"' or a[0:1] == "'":
            # strip enclosing quotes and unquote interior
            a = a[1:-1].replace(a[0] * 2, a[0])
        strip_values.append(a)
    return strip_values
