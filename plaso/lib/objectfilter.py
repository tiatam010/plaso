"""Classes to perform filtering of objects based on their data members.

Given a list of objects and a textual filter expression, these classes allow
you to determine which objects match the filter. The system has two main
pieces: A parser for the supported grammar and a filter implementation.

Given any complying user-supplied grammar, it is parsed with a custom lexer
based on GRR's lexer and then compiled into an actual implementation by using
the filter implementation. A filter implementation simply provides actual
implementations for the primitives required to perform filtering. The compiled
result is always a class supporting the Filter interface.

If we define a class called Car such as:


class Car(object):
  def __init__(self, code, color="white", doors=3):
    self.code = code
    self.color = color
    self.doors = 3

And we have two instances:

  ford_ka = Car("FORDKA1", color="grey")
  toyota_corolla = Car("COROLLA1", color="white", doors=5)
  fleet = [ford_ka, toyota_corolla]

We want to find cars that are grey and have 3 or more doors. We could filter
our fleet like this:

  criteria = "(color is grey) and (doors >= 3)"
  parser = ContextFilterParser(criteria).Parse()
  compiled_filter = parser.Compile(LowercaseAttributeFilterImp)

  for car in fleet:
    if compiled_filter.Matches(car):
      print("Car %s matches the supplied filter." % car.code)

The filter expression contains two subexpressions joined by an AND operator:
  "color is grey" and "doors >= 3"

This means we want to search for objects matching these two subexpressions.
Let's analyze the first one in depth "color is grey":

  "color": the left operand specifies a search path to look for the data. This
  tells our filtering system to look for the color property on passed objects.
  "is": the operator. Values retrieved for the "color" property will be checked
  against the right operand to see if they are equal.
  "grey": the right operand. It specifies an explicit value to check for.

So each time an object is passed through the filter, it will expand the value
of the color data member, and compare its value against "grey".

Because data members of objects are often not simple datatypes but other
objects, the system allows you to reference data members within other data
members by separating each by a dot. Let's see an example:

Let's add a more complex Car class with default tyre data:


class CarWithTyres(Car):
  def __init__(self, code, tyres=None, color="white", doors=3):
    super(self, CarWithTyres).__init__(code, color, doors)
    tyres = tyres or Tyre("Pirelli", "PZERO")


class Tyre(object):
  def __init__(self, brand, code):
    self.brand = brand
    self.code = code

And two new instances:
  ford_ka = CarWithTyres("FORDKA", color="grey", tyres=Tyre("AVON", "ZT5"))
  toyota_corolla = Car("COROLLA1", color="white", doors=5)
  fleet = [ford_ka, toyota_corolla]

To filter a car based on the tyre brand, we would use a search path of
"tyres.brand".

Because the filter implementation provides the actual classes that perform
handling of the search paths, operators, etc. customizing the behavior of the
filter is easy. Three basic filter implementations are given:

  BaseFilterImplementation: search path expansion is done on attribute names
  as provided (case-sensitive).
  LowercaseAttributeFilterImp: search path expansion is done on the lowercased
  attribute name, so that it only accesses attributes, not methods.
  DictFilterImplementation: search path expansion is done on dictionary access
  to the given object. So "a.b" expands the object obj to obj["a"]["b"]
"""

from __future__ import unicode_literals

import abc
import binascii
import codecs
import logging
import re

from plaso.lib import errors
from plaso.lib import py2to3


def GetUnicodeString(value):
  """Attempts to convert the argument to a Unicode string.

  Args:
    value (list|int|bytes|str): value to convert.

  Returns:
    str: string representation of the argument.
  """
  if isinstance(value, list):
    value = [GetUnicodeString(item) for item in value]
    return ''.join(value)

  if isinstance(value, py2to3.INTEGER_TYPES):
    value = '{0:d}'.format(value)

  if not isinstance(value, py2to3.UNICODE_TYPE):
    return codecs.decode(value, 'utf8', 'ignore')
  return value


class Token(object):
  """An event filter parser token.

  Attributes:
    actions (list[str]): list of method names in the SearchParser to call.
    next_state (str): next state we transition to if this Token matches.
    re_str (str): regular expression to try and match from the current point.
    regex (_sre.SRE_Pattern): regular expression to try and match from
        the current point.
    state_regex (str): regular expression that is considered when the current
        state matches this rule.
  """

  def __init__(self, state_regex, regex, actions, next_state, flags=re.I):
    """Initializes an event filter expressions parser token.

    Args:
      state_regex (str): regular expression that is considered when the current
          state matches this rule.
      regex (str): regular expression to try and match from the current point.
      actions (list[str]): list of method names in the SearchParser to call.
      next_state (str): next state we transition to if this Token matches.
      flags (Optional[int]): flags for the regular expression module (re).
    """
    super(Token, self).__init__()
    self.actions = []
    self.next_state = next_state
    self.re_str = regex
    self.regex = re.compile(regex, re.DOTALL | re.M | re.S | re.U | flags)
    self.state_regex = re.compile(
        state_regex, re.DOTALL | re.M | re.S | re.U | flags)

    if actions:
      self.actions = actions.split(',')


class Expression(object):
  """An event filter parser expression.

  Attributes:
    attribute (str): attribute or None if not set.
    args (list[str]): arguments.
    number_of_args (int): expected number of arguments.
    operator (str): operator or None if not set.
  """

  # TODO: this currently needs to be a class attribute for objectfilter.
  # See if this can be changed to an instance attribute.
  attribute = None

  def __init__(self):
    """Initializes an event filter parser expression."""
    super(Expression, self).__init__()
    self.args = []
    self.number_of_args = 1
    self.operator = None

  def AddArg(self, argument):
    """Adds a new argument to this expression.

    Args:
       argument (str): argument to add.

    Returns:
      bool: True if the argument is the last argument, False otherwise.

    Raises:
      ParseError: If there are too many arguments.
    """
    self.args.append(argument)
    if len(self.args) > self.number_of_args:
      raise errors.ParseError('Too many arguments for this expression.')

    elif len(self.args) == self.number_of_args:
      return True

    return False

  @abc.abstractmethod
  def Compile(self, filter_implementation):
    """Given a filter implementation, compile this expression.

    Args:
      filter_implementation (type): class of the filter object, which should
          be a subclass of objectfilter.BaseFilterImplementation.

    Returns:
      object: filter object of the binary expression.
    """

  def SetAttribute(self, attribute):
    """Sets the attribute.

    Args:
      attribute (str): attribute, or None if not set.
    """
    self.attribute = attribute

  def SetOperator(self, operator):
    """Set the operator.

    Args:
      operator (str): operator, such as "and" or "&&", or None if not set.
    """
    self.operator = operator


class BinaryExpression(Expression):
  """An event filter parser expression which takes two other expressions."""

  def __init__(self, operator='', part=None):
    """Initializes an event filter parser binary expression.

    Args:
      operator (str): operator, such as "and" or "&&".
      part (str): expression part.
    """
    super(BinaryExpression, self).__init__()
    self.args = []
    self.operator = operator

    if part:
      self.args.append(part)

  def AddOperands(self, lhs, rhs):
    """Adds an operand.

    Args:
      lhs (Expression): left hand side expression.
      rhs (Expression): right hand side expression.

    Raises:
      ParseError: if either left hand side or right hand side expression
          is not an instance of Expression.
    """
    if not isinstance(lhs, Expression):
      raise errors.ParseError('Left hand side is not an expression')

    if not isinstance(rhs, Expression):
      raise errors.ParseError('Right hand side is not an expression')

    self.args = [lhs, rhs]

  def Compile(self, filter_implementation):
    """Compiles the binary expression into a filter object.

    Args:
      filter_implementation (type): class of the filter object, which should
          be a subclass of objectfilter.BaseFilterImplementation.

    Returns:
      object: filter object of the binary expression.
    """
    operator = self.operator.lower()
    if operator in ('and', '&&'):
      method = 'AndFilter'
    elif operator in ('or', '||'):
      method = 'OrFilter'
    else:
      raise errors.ParseError(
          'Invalid binary operator {0:s}.'.format(operator))

    args = [x.Compile(filter_implementation) for x in self.args]
    return filter_implementation.FILTERS[method](arguments=args)


class IdentityExpression(Expression):
  """An event filter parser expression which always evaluates to True."""

  def Compile(self, filter_implementation):
    """Compiles the binary expression into a filter object.

    Args:
      filter_implementation (type): class of the filter object, which should
          be a subclass of objectfilter.BaseFilterImplementation.

    Returns:
      object: filter object of the identity expression.
    """
    return filter_implementation.IdentityFilter()


# pylint: disable=attribute-defined-outside-init
# pylint: disable=missing-docstring,missing-param-doc
# pylint: disable=missing-type-doc,missing-yield-type-doc


class Filter(object):
  """Base class for every filter."""

  def __init__(self, arguments=None, value_expander=None):
    """Constructor.

    Args:
      arguments: Arguments to the filter.
      value_expander: A callable that will be used to expand values for the
      objects passed to this filter. Implementations expanders are provided by
      subclassing ValueExpander.

    Raises:
      ValueError: If the given value_expander is not a subclass of ValueExpander
    """
    self.value_expander = None
    self.value_expander_cls = value_expander
    if self.value_expander_cls:
      if not issubclass(self.value_expander_cls, ValueExpander):
        raise ValueError('{0:s} is not a valid value expander'.format(
            self.value_expander_cls))
      self.value_expander = self.value_expander_cls()
    self.args = arguments or []
    logging.debug('Adding {0!s}'.format(arguments))

  @abc.abstractmethod
  def Matches(self, obj):
    """Whether object obj matches this filter."""

  def Filter(self, objects):
    """Returns a list of objects that pass the filter."""
    return filter(self.Matches, objects)

  def __str__(self):
    return '{0:s}({1:s})'.format(self.__class__.__name__, ', '.join([
        str(argument) for argument in self.args]))


class AndFilter(Filter):
  """Performs a boolean AND of the given Filter instances as arguments.

    Note that if no conditions are passed, all objects will pass.
  """
  def Matches(self, obj):
    for child_filter in self.args:
      if not child_filter.Matches(obj):
        return False
    return True


class OrFilter(Filter):
  """Performs a boolean OR of the given Filter instances as arguments.

  Note that if no conditions are passed, all objects will pass.
  """
  def Matches(self, obj):
    if not self.args:
      return True

    for child_filter in self.args:
      if child_filter.Matches(obj):
        return True
    return False


class Operator(Filter):
  """Base class for all operators."""

  @abc.abstractmethod
  def Matches(self, obj):
    """Whether object obj matches this filter."""


class IdentityFilter(Operator):
  def Matches(self, _):
    return True


class UnaryOperator(Operator):
  """Base class for unary operators."""

  def __init__(self, operand, **kwargs):
    """Constructor."""
    super(UnaryOperator, self).__init__(arguments=[operand], **kwargs)
    if len(self.args) != 1:
      raise errors.InvalidNumberOfOperands(
          'Only one operand is accepted by {0:s}. Received {1:d}.'.format(
              self.__class__.__name__, len(self.args)))

  @abc.abstractmethod
  def Matches(self, obj):
    """Whether object obj matches this filter."""


class BinaryOperator(Operator):
  """Base class for binary operators.

  The left operand is always a path into the object which will be expanded for
  values. The right operand is a value defined at initialization and is stored
  at self.right_operand.
  """
  def __init__(self, arguments=None, **kwargs):
    super(BinaryOperator, self).__init__(arguments=arguments, **kwargs)
    if len(self.args) != 2:
      raise errors.InvalidNumberOfOperands(
          'Only two operands are accepted by {0:s}. Received {1:s}.'.format(
              self.__class__.__name__, len(self.args)))

    self.left_operand = self.args[0]
    self.right_operand = self.args[1]

  @abc.abstractmethod
  def Matches(self, obj):
    """Whether object obj matches this filter."""


class GenericBinaryOperator(BinaryOperator):
  """Allows easy implementations of operators."""

  def __init__(self, **kwargs):
    super(GenericBinaryOperator, self).__init__(**kwargs)
    self.bool_value = True

  def FlipBool(self):
    logging.debug('Negative matching.')
    self.bool_value = not self.bool_value

  def Operation(self, x, y):
    """Performs the operation between two values."""

  def Operate(self, values):
    """Takes a list of values and if at least one matches, returns True."""
    for val in values:
      try:
        if self.Operation(val, self.right_operand):
          return True
      except (TypeError, ValueError):
        pass

    return False

  def Matches(self, obj):
    key = self.left_operand
    values = self.value_expander.Expand(obj, key)
    values = list(values)
    if values and self.Operate(values):
      return self.bool_value
    return not self.bool_value


class Equals(GenericBinaryOperator):
  """Matches objects when the right operand equals the expanded value."""

  def Operation(self, x, y):
    return x == y


class NotEquals(Equals):
  """Matches when the right operand isn't equal to the expanded value."""

  def __init__(self, **kwargs):
    super(NotEquals, self).__init__(**kwargs)
    self.bool_value = False


class Less(GenericBinaryOperator):
  """Whether the expanded value >= right_operand."""

  def Operation(self, x, y):
    return x < y


class LessEqual(GenericBinaryOperator):
  """Whether the expanded value <= right_operand."""

  def Operation(self, x, y):
    return x <= y


class Greater(GenericBinaryOperator):
  """Whether the expanded value > right_operand."""

  def Operation(self, x, y):
    return x > y


class GreaterEqual(GenericBinaryOperator):
  """Whether the expanded value >= right_operand."""

  def Operation(self, x, y):
    return x >= y


class Contains(GenericBinaryOperator):
  """Whether the right operand is contained in the value."""

  def Operation(self, x, y):
    if isinstance(x, py2to3.STRING_TYPES):
      return y.lower() in x.lower()

    return y in x


class InSet(GenericBinaryOperator):
  # TODO: Change to an N-ary Operator?

  """Whether all values are contained within the right operand."""

  def Operation(self, x, y):
    """Whether x is fully contained in y."""
    if x in y:
      return True

    # x might be an iterable
    # first we need to skip strings or we'll do silly things
    # pylint: disable=consider-merging-isinstance
    if isinstance(x, py2to3.STRING_TYPES) or isinstance(x, bytes):
      return False

    try:
      for value in x:
        if value not in y:
          return False
      return True
    except TypeError:
      return False


class Regexp(GenericBinaryOperator):
  """Whether the value matches the regexp in the right operand."""

  def __init__(self, *children, **kwargs):
    super(Regexp, self).__init__(*children, **kwargs)
    # Note that right_operand is not necessarily a string.
    logging.debug('Compiled: {0!s}'.format(self.right_operand))
    try:
      self.compiled_re = re.compile(
          GetUnicodeString(self.right_operand), re.DOTALL)
    except re.error:
      raise ValueError('Regular expression "{0!s}" is malformed.'.format(
          self.right_operand))

  def Operation(self, x, unused_y):
    try:
      if self.compiled_re.search(GetUnicodeString(x)):
        return True
    except TypeError:
      pass

    return False


class RegexpInsensitive(Regexp):
  """Whether the value matches the regexp in the right operand."""

  def __init__(self, *children, **kwargs):
    super(RegexpInsensitive, self).__init__(*children, **kwargs)
    # Note that right_operand is not necessarily a string.
    logging.debug('Compiled: {0!s}'.format(self.right_operand))
    try:
      self.compiled_re = re.compile(GetUnicodeString(self.right_operand),
                                    re.I | re.DOTALL)
    except re.error:
      raise ValueError('Regular expression "{0!s}" is malformed.'.format(
          self.right_operand))


class Context(Operator):
  """Restricts the child operators to a specific context within the object.

  Solves the context problem. The context problem is the following:
  Suppose you store a list of loaded DLLs within a process. Suppose that for
  each of these DLLs you store the number of imported functions and each of the
  imported functions name.

  Imagine that a malicious DLL is injected into processes and its indicators are
  that it only imports one function and that it is RegQueryValueEx. Yo'd write
  your indicator like this:


  AndOperator(
    Equal("ImportedDLLs.ImpFunctions.Name", "RegQueryValueEx"),
    Equal("ImportedDLLs.NumImpFunctions", "1")
    )

  Now imagine you have these two processes on a given system.

  Process1
  * __ImportedDlls

    * __Name: "notevil.dll"

      * __ImpFunctions

        * __Name: "CreateFileA"

      * __NumImpFunctions: 1

    * __Name: "alsonotevil.dll"

      * __ImpFunctions

        * __Name: "RegQueryValueEx"
        * __Name: "CreateFileA"

      * __NumImpFunctions: 2

  Process2
  * __ImportedDlls

    * __Name: "evil.dll"

      * __ImpFunctions

        * __Name: "RegQueryValueEx"

      * __NumImpFunctions: 1

  Both Process1 and Process2 match your query, as each of the indicators are
  evaluated separately. While you wanted to express "find me processes that
  have a DLL that has both one imported function and ReqQueryValueEx is in the
  list of imported functions", your indicator actually means "find processes
  that have at least a DLL with 1 imported functions and at least one DLL that
  imports the ReqQueryValueEx function".

  To write such an indicator you need to specify a context of ImportedDLLs for
  these two clauses. Such that you convert your indicator to::

      Context("ImportedDLLs",
              AndOperator(
                Equal("ImpFunctions.Name", "RegQueryValueEx"),
                Equal("NumImpFunctions", "1")
              ))

  Context will execute the filter specified as the second parameter for each of
  the objects under "ImportedDLLs", thus applying the condition per DLL, not per
  object and returning the right result.
  """

  def __init__(self, arguments=None, **kwargs):
    if len(arguments) != 2:
      raise errors.InvalidNumberOfOperands('Context accepts only 2 operands.')
    super(Context, self).__init__(arguments=arguments, **kwargs)
    self.context, self.condition = self.args

  def Matches(self, obj):
    for object_list in self.value_expander.Expand(obj, self.context):
      for sub_object in object_list:
        if self.condition.Matches(sub_object):
          return True
    return False


class ValueExpander(object):
  """Encapsulates the logic to expand values available in an object.

  Once instantiated and called, this class returns all the values that follow a
  given field path.
  """

  FIELD_SEPARATOR = '.'

  def _GetAttributeName(self, path):
    """Returns the attribute name to fetch given a path."""
    return path[0]

  def _GetValue(self, unused_obj, unused_attr_name):
    """Returns the value of tha attribute attr_name."""
    raise NotImplementedError()

  def _AtLeaf(self, attr_value):
    """Called when at a leaf value. Should yield a value."""
    yield attr_value

  def _AtNonLeaf(self, attr_value, path):
    """Called when at a non-leaf value. Should recurse and yield values."""
    try:
      # Check first for iterables
      # If it's a dictionary, we yield it
      if isinstance(attr_value, dict):
        yield attr_value
      else:
        # If it's an iterable, we recurse on each value.
        for sub_obj in attr_value:
          for value in self.Expand(sub_obj, path[1:]):
            yield value
    except TypeError:  # This is then not iterable, we recurse with the value
      for value in self.Expand(attr_value, path[1:]):
        yield value

  def Expand(self, obj, path):
    """Returns a list of all the values for the given path in the object obj.

    Given a path such as ["sub1", "sub2"] it returns all the values available
    in obj.sub1.sub2 as a list. sub1 and sub2 must be data attributes or
    properties.

    If sub1 returns a list of objects, or a generator, Expand aggregates the
    values for the remaining path for each of the objects, thus returning a
    list of all the values under the given path for the input object.

    Args:
      obj: An object that will be traversed for the given path
      path: A list of strings

    Yields:
      The values once the object is traversed.
    """
    if isinstance(path, py2to3.STRING_TYPES):
      path = path.split(self.FIELD_SEPARATOR)

    attr_name = self._GetAttributeName(path)
    attr_value = self._GetValue(obj, attr_name)
    if attr_value is None:
      return

    if len(path) == 1:
      for value in self._AtLeaf(attr_value):
        yield value
    else:
      for value in self._AtNonLeaf(attr_value, path):
        yield value


class AttributeValueExpander(ValueExpander):
  """An expander that gives values based on object attribute names."""

  def _GetValue(self, obj, attr_name):
    return getattr(obj, attr_name, None)


class LowercaseAttributeValueExpander(AttributeValueExpander):
  """An expander that lowercases all attribute names before access."""

  def _GetAttributeName(self, path):
    return path[0].lower()


class DictValueExpander(ValueExpander):
  """An expander that gets values from dictionary access to the object."""

  def _GetValue(self, obj, attr_name):
    return obj.get(attr_name, None)


class BasicExpression(Expression):
  """Basic Expression."""

  def __init__(self):
    super(BasicExpression, self).__init__()
    self.bool_value = True

  def FlipBool(self):
    self.bool_value = not self.bool_value

  def Compile(self, filter_implementation):
    arguments = [self.attribute]
    op_str = self.operator.lower()
    operator = filter_implementation.OPS.get(op_str, None)

    if not operator:
      raise errors.ParseError('Unknown operator {0:s} provided.'.format(
          self.operator))

    arguments.extend(self.args)
    expander = filter_implementation.FILTERS['ValueExpander']
    ops = operator(arguments=arguments, value_expander=expander)
    if not self.bool_value:
      if hasattr(ops, 'FlipBool'):
        ops.FlipBool()

    return ops


class ContextExpression(Expression):
  """Represents the context operator."""

  def __init__(self, attribute="", part=None):
    self.attribute = attribute
    self.args = []
    if part:
      self.args.append(part)
    super(ContextExpression, self).__init__()

  def __str__(self):
    return 'Context({0:s} {1:s})'.format(
        self.attribute, [str(x) for x in self.args])

  def SetExpression(self, expression):
    """Set the expression."""
    if isinstance(expression, Expression):
      self.args = [expression]
    else:
      raise errors.ParseError(
          'Expected expression, got {0:s}.'.format(expression))

  def Compile(self, filter_implementation):
    """Compile the expression."""
    arguments = [self.attribute]
    for argument in self.args:
      arguments.append(argument.Compile(filter_implementation))
    expander = filter_implementation.FILTERS['ValueExpander']
    context_cls = filter_implementation.FILTERS['Context']
    return context_cls(arguments=arguments,
                       value_expander=expander)


class Parser(object):
  """A parser for objectfilter expressions.

  Examples of valid syntax:
    size is 40
    (name contains "Program Files" AND hash.md5 is "123abc")
    @imported_modules (num_symbols = 14 AND symbol.name is "FindWindow")

  Attributes:
    buffer (str): buffer that holds the expression.
    current_expression (Expression): current expression.
    error (int): ???
    filter_string (str): ???
    flags (int): ???
    processed (int): ???
    processed_buffer (str): buffer that holds the part of the expression
        that has been processed.
    stack (list[str]): token stack.
    state (str): parser state.
    state_stack (list[str]): stack of parser states.
    string (str): string expression or None if not set.
  """
  _CONTINUE_STATE = 'CONTINUE'
  _INITIAL_STATE = 'INITIAL'

  _ERROR_TOKEN = 'Error'

  # Classes of the expressions generated by the parser.
  expression_cls = BasicExpression
  binary_expression_cls = BinaryExpression
  context_cls = ContextExpression

  tokens = [
      # Operators and related tokens
      Token('INITIAL', r'\@[\w._0-9]+', 'ContextOperator,PushState',
            'CONTEXTOPEN'),
      Token('INITIAL', r'[^\s\(\)]', 'PushState,PushBack', 'ATTRIBUTE'),
      Token('INITIAL', r'\(', 'PushState,BracketOpen', None),
      Token('INITIAL', r'\)', 'BracketClose', 'BINARY'),

      # Context
      Token('CONTEXTOPEN', r'\(', 'BracketOpen', 'INITIAL'),

      # Double quoted string
      Token('STRING', '"', 'PopState,StringFinish', None),
      Token('STRING', r'\\x(..)', 'HexEscape', None),
      Token('STRING', r'\\(.)', 'StringEscape', None),
      Token('STRING', r'[^\\"]+', 'StringInsert', None),

      # Single quoted string
      Token('SQ_STRING', '\'', 'PopState,StringFinish', None),
      Token('SQ_STRING', r'\\x(..)', 'HexEscape', None),
      Token('SQ_STRING', r'\\(.)', 'StringEscape', None),
      Token('SQ_STRING', r'[^\\\']+', 'StringInsert', None),

      # Basic expression
      Token('ATTRIBUTE', r'[\w._0-9]+', 'StoreAttribute', 'OPERATOR'),
      Token('OPERATOR', r'not ', 'FlipLogic', None),
      Token('OPERATOR', r'(\w+|[<>!=]=?)', 'StoreOperator', 'CHECKNOT'),
      Token('CHECKNOT', r'not', 'FlipLogic', 'ARG'),
      Token('CHECKNOT', r'\s+', None, None),
      Token('CHECKNOT', r'([^not])', 'PushBack', 'ARG'),
      Token('ARG', r'(\d+\.\d+)', 'InsertFloatArg', 'ARG'),
      Token('ARG', r'(0x\d+)', 'InsertInt16Arg', 'ARG'),
      Token('ARG', r'(\d+)', 'InsertIntArg', 'ARG'),
      Token('ARG', '"', 'PushState,StringStart', 'STRING'),
      Token('ARG', '\'', 'PushState,StringStart', 'SQ_STRING'),
      # When the last parameter from arg_list has been pushed

      # State where binary operators are supported (AND, OR)
      Token('BINARY', r'(?i)(and|or|\&\&|\|\|)', 'BinaryOperator', 'INITIAL'),
      # - We can also skip spaces
      Token('BINARY', r'\s+', None, None),
      # - But if it's not "and" or just spaces we have to go back
      Token('BINARY', '.', 'PushBack,PopState', None),

      # Skip whitespace.
      Token('.', r'\s+', None, None)]

  def __init__(self, data=''):
    """Initializes an objectfilter parser.

    Args:
      data (str): initial data to be processed by the parser.
    """
    super(Parser, self).__init__()
    self.buffer = data
    self.current_expression = self.expression_cls()
    self.error = 0
    self.filter_string = data
    self.flags = 0
    self.processed = 0
    self.processed_buffer = ''
    self.stack = []
    self.state = self._INITIAL_STATE
    self.state_stack = []
    self.string = None

  def _CombineContext(self):
    """Combines context."""
    # Context can merge from item 0
    for i in range(len(self.stack)-1, 0, -1):
      item = self.stack[i-1]
      if (isinstance(item, ContextExpression) and
          isinstance(self.stack[i], Expression)):
        expression = self.stack[i]
        self.stack[i-1].SetExpression(expression)
        self.stack[i] = None

    self.stack = list(filter(None, self.stack))

  def _CombineBinaryExpressions(self, operator):
    """Combines binary expressions.

    Args:
      operator (str): operator, such as "and" or "&&".
    """
    for i in range(1, len(self.stack)-1):
      item = self.stack[i]
      if (isinstance(item, BinaryExpression) and
          item.operator.lower() == operator.lower() and
          isinstance(self.stack[i-1], Expression) and
          isinstance(self.stack[i+1], Expression)):
        lhs = self.stack[i-1]
        rhs = self.stack[i+1]

        self.stack[i].AddOperands(lhs, rhs)
        self.stack[i-1] = None
        self.stack[i+1] = None

    self.stack = list(filter(None, self.stack))

  def _CombineParenthesis(self):
    """Combines parenthesis."""
    for i in range(len(self.stack)-2):
      if (self.stack[i] == '(' and self.stack[i+2] == ')' and
          isinstance(self.stack[i+1], Expression)):
        self.stack[i] = None
        self.stack[i+2] = None

    self.stack = list(filter(None, self.stack))

  def BinaryOperator(self, string=None, **unused_kwargs):
    """Sets a binary operator.

    Args:
      string (str): operator, such as "and" or "&&".
    """
    expression = self.binary_expression_cls(operator=string)
    self.stack.append(expression)

  def BracketClose(self, **unused_kwargs):
    """Defines a closing bracket."""
    self.stack.append(')')

  def BracketOpen(self, **unused_kwargs):
    """Defines an open bracket."""
    self.stack.append('(')

  def Close(self):
    """Forces parse the remaining data in the buffer."""
    while self.NextToken():
      if not self.buffer:
        return

  def ContextOperator(self, string='', **unused_kwargs):
    """Sets a context operator.

    Args:
      string (str): operator.
    """
    self.stack.append(self.context_cls(string[1:]))

  def Default(self, **kwarg):
    """Default callback handler."""
    logging.debug('Default handler: {0!s}'.format(kwarg))

  def Empty(self):
    """Checks if the buffer is empty.

    Returns:
      bool: True if the buffer is emtpy.
    """
    return not self.buffer

  def Error(self, message=None, weight=1):  # pylint: disable=unused-argument
    """Raises a parse error.

    Args:
      message (Optional[str]): error message.
      weight (Optional[int]): error weight.

    Raises:
      ParseError: always raised.
    """
    # Note that none of the values necessarily are strings.
    raise errors.ParseError(
        '{0!s} in position {1!s}: {2!s} <----> {3!s} )'.format(
            message, len(self.processed_buffer), self.processed_buffer,
            self.buffer))

  def Feed(self, data):
    """Feeds the buffer with data.

    Args:
      data (str): data to be processed by the parser.
    """
    self.buffer = ''.join([self.buffer, data])

  def FlipAllowed(self):
    """Raise an error if the not keyword is used where it is not allowed."""
    if not hasattr(self, 'flipped'):
      raise errors.ParseError('Not defined.')

    if not self.flipped:
      return

    if self.current_expression.operator:
      if not self.current_expression.operator.lower() in (
          'is', 'contains', 'inset', 'equals'):
        raise errors.ParseError(
            'Keyword \'not\' does not work against operator: {0:s}'.format(
                self.current_expression.operator))

  def FlipLogic(self, **unused_kwargs):
    """Flip the boolean logic of the expression.

    If an expression is configured to return True when the condition
    is met this logic will flip that to False, and vice versa.
    """
    if hasattr(self, 'flipped') and self.flipped:
      raise errors.ParseError(
          'The operator \'not\' can only be expressed once.')

    if self.current_expression.args:
      raise errors.ParseError(
          'Unable to place the keyword \'not\' after an argument.')

    self.flipped = True

    # Check if this flip operation should be allowed.
    self.FlipAllowed()

    if hasattr(self.current_expression, 'FlipBool'):
      self.current_expression.FlipBool()
      logging.debug('Negative matching [flipping boolean logic].')
    else:
      logging.warning(
          'Unable to perform a negative match, issuing a positive one.')

  def HexEscape(self, string, match, **unused_kwargs):
    """Converts a hex escaped string."""
    logging.debug('HexEscape matched {0:s}.'.format(string))
    hex_string = match.group(1)
    try:
      hex_string = binascii.unhexlify(hex_string)
      hex_string = codecs.decode(hex_string, 'utf-8')
      self.string += hex_string
    except (TypeError, binascii.Error):
      raise errors.ParseError('Invalid hex escape {0!s}.'.format(hex_string))

  def InsertArg(self, string='', **unused_kwargs):
    """Inserts an argument into the current expression.

    Args:
      string (Optional[str]): argument string.

    Returns:
      str: state or None if the argument could not be added to the current
          expression.
    """
    # Note that "string" is not necessarily of type string.
    logging.debug('Storing argument: {0!s}'.format(string))

    # Check if this flip operation should be allowed.
    self.FlipAllowed()

    # This expression is complete
    if self.current_expression.AddArg(string):
      self.stack.append(self.current_expression)
      self.current_expression = self.expression_cls()
      # We go to the BINARY state, to find if there's an AND or OR operator
      return 'BINARY'

    return None

  def InsertFloatArg(self, string='', **unused_kwargs):
    """Inserts a floating-point argument into the current expression.

    Args:
      string (Optional[str]): argument string that contains a floating-point
          value.

    Returns:
      str: state or None if the argument could not be added to the current
          expression.
    """
    try:
      float_value = float(string)
    except (TypeError, ValueError):
      raise errors.ParseError('{0:s} is not a valid float.'.format(string))
    return self.InsertArg(float_value)

  def InsertIntArg(self, string='', **unused_kwargs):
    """Inserts a decimal integer argument into the current expression.

    Args:
      string (Optional[str]): argument string that contains an integer value
          formatted in decimal.

    Returns:
      str: state or None if the argument could not be added to the current
          expression.
    """
    try:
      int_value = int(string)
    except (TypeError, ValueError):
      raise errors.ParseError('{0:s} is not a valid integer.'.format(string))
    return self.InsertArg(int_value)

  def InsertInt16Arg(self, string='', **unused_kwargs):
    """Inserts a hexadecimal integer argument into the current expression.

    Args:
      string (Optional[str]): argument string that contains an integer value
          formatted in hexadecimal.

    Returns:
      str: state or None if the argument could not be added to the current
          expression.
    """
    try:
      int_value = int(string, 16)
    except (TypeError, ValueError):
      raise errors.ParseError(
          '{0:s} is not a valid base16 integer.'.format(string))
    return self.InsertArg(int_value)

  def NextToken(self):
    """Fetches the next token by trying to match any of the regexes in order.

    Returns:
      str: token.
    """
    current_state = self.state
    for token in self.tokens:
      # Does the rule apply to us?
      if not token.state_regex.match(current_state):
        continue

      # Try to match the rule
      m = token.regex.match(self.buffer)
      if not m:
        continue

      # The match consumes the data off the buffer (the handler can put it back
      # if it likes)
      # TODO: using joins might be more efficient here.
      self.processed_buffer += self.buffer[:m.end()]
      self.buffer = self.buffer[m.end():]
      self.processed += m.end()

      next_state = token.next_state
      for action in token.actions:

        # Is there a callback to handle this action?
        callback = getattr(self, action, self.Default)

        # Allow a callback to skip other callbacks.
        try:
          possible_next_state = callback(string=m.group(0), match=m)
          if possible_next_state == self._CONTINUE_STATE:
            continue
          # Override the state from the Token
          elif possible_next_state:
            next_state = possible_next_state
        except errors.ParseError as exception:
          self.Error(exception)

      # Update the next state
      if next_state:
        self.state = next_state

      return token

    # Check that we are making progress - if we are too full, we assume we are
    # stuck.
    self.Error('Expected {0:s}'.format(self.state))
    self.processed_buffer += self.buffer[:1]
    self.buffer = self.buffer[1:]
    return self._ERROR_TOKEN

  def Parse(self):
    """Parses the data in the internal buffer.

    Returns:
      Expression: expression.
    """
    if not self.filter_string:
      return IdentityExpression()

    self.Close()
    return self.Reduce()

  def PopState(self, **unused_kwargs):
    """Pops the previous state from the stack.

    Returns:
      str: parser state.
    """
    try:
      self.state = self.state_stack.pop()
      logging.debug('Returned state to {0:s}'.format(self.state))

      return self.state
    except IndexError:
      self.Error('Tried to pop the state but failed - possible recursion error')

  def PushBack(self, string='', **unused_kwargs):
    """Pushes the match back on the stream.

    Args:
      string (Optional[str]): expression string.
    """
    self.buffer = string + self.buffer
    self.processed_buffer = self.processed_buffer[:-len(string)]

  def PushState(self, **unused_kwargs):
    """Pushes the current state on the state stack."""
    logging.debug('Storing state {0:s}'.format(repr(self.state)))
    self.state_stack.append(self.state)

  def Reduce(self):
    """Reduce the token stack into an abstract syntax tree (AST).

    Returns:
      Expression: first expression in the AST.
    """
    # Check for sanity
    if self.state != 'INITIAL' and self.state != 'BINARY':
      self.Error('Premature end of expression')

    length = len(self.stack)
    while length > 1:
      # Precedence order
      self._CombineParenthesis()
      self._CombineBinaryExpressions('and')
      self._CombineBinaryExpressions('or')
      self._CombineContext()

      # No change
      if len(self.stack) == length:
        break
      length = len(self.stack)

    if length != 1:
      self.Error('Illegal query expression')

    return self.stack[0]

  def StringEscape(self, string, match, **unused_kwargs):
    """Escapes backslashes found inside an expression string.

    Backslashes followed by anything other than [\'"rnbt.ws] will raise
    an Error.

    Args:
      string: The string that matched.
      match: the match object (instance of re.MatchObject).
             Where match.group(1) contains the escaped code.

    Raises:
      ParseError: When the escaped string is not one of [\'"rnbt]
    """
    if match.group(1) in '\\\'"rnbt\\.ws':
      self.string += codecs.decode(string, 'unicode_escape')
    else:
      raise errors.ParseError('Invalid escape character {0:s}.'.format(string))

  def StringFinish(self, **unused_kwargs):
    """Finishes a string operation.

    Returns:
      str: token or None when the internal state is not "ATTRIBUTE" or
          "ARG".
    """
    if self.state == 'ATTRIBUTE':
      return self.StoreAttribute(string=self.string)

    if self.state == 'ARG':
      return self.InsertArg(string=self.string)

    return None

  def StringInsert(self, string='', **unused_kwargs):
    """Adds the expression string to the internal string.

    Args:
      string (Optional[str]): expression string.
    """
    self.string += string

  def StringStart(self, **unused_kwargs):
    """Initializes the internal string."""
    self.string = ''

  def StoreAttribute(self, string='', **unused_kwargs):
    """Store the attribute.

    Args:
      string (Optional[str]): expression string.

    Returns:
      str: token.
    """
    logging.debug('Storing attribute {0:s}'.format(repr(string)))

    self.flipped = False

    # TODO: Update the expected number_of_args
    try:
      self.current_expression.SetAttribute(string)
    except AttributeError:
      raise errors.ParseError('Invalid attribute \'{0:s}\''.format(string))

    return 'OPERATOR'

  def StoreOperator(self, string='', **unused_kwargs):
    """Store the operator.

    Args:
      string (Optional[str]): expression string.
    """
    logging.debug('Storing operator {0:s}'.format(repr(string)))
    self.current_expression.SetOperator(string)


# Filter implementations.


class BaseFilterImplementation(object):
  """Defines the base implementation of an object filter by its attributes.

  Inherit from this class, switch any of the needed operators and pass it to
  the Compile method of a parsed string to obtain an executable filter.
  """

  OPS = {
      'equals': Equals,
      'is': Equals,
      '==': Equals,
      '!=': NotEquals,
      'contains': Contains,
      '>': Greater,
      '>=': GreaterEqual,
      '<': Less,
      '<=': LessEqual,
      'inset': InSet,
      'regexp': Regexp,
      'iregexp': RegexpInsensitive}

  FILTERS = {
      'ValueExpander': AttributeValueExpander,
      'AndFilter': AndFilter,
      'OrFilter': OrFilter,
      'IdentityFilter': IdentityFilter,
      'Context': Context}
