# -*- coding: utf-8 -*-
# Copyright: See the LICENSE file.

from __future__ import unicode_literals

import collections
import itertools
import logging

from . import compat
from . import utils


logger = logging.getLogger('factory.generate')


class BaseDeclaration(object):
    """A factory declaration.

    Ordered declarations mark an attribute as needing lazy evaluation.
    This allows them to refer to attributes defined by other BaseDeclarations
    in the same factory.
    """

    creation_counter = 0

    def __init__(self, **kwargs):
        super(BaseDeclaration, self).__init__(**kwargs)
        self.creation_counter = BaseDeclaration.creation_counter
        BaseDeclaration.creation_counter += 1

    def evaluate(self, instance, step, extra):
        """Evaluate this declaration.

        Args:
            instance (builder.Resolver): The object holding currently computed
                attributes
            step: a factory.builder.BuildStep
            extra (dict): additional, call-time added kwargs
                for the step.
        """
        raise NotImplementedError('This is an abstract method')


class OrderedDeclaration(BaseDeclaration):
    """Compatibility"""

    # FIXME(rbarrois)


class LazyFunction(BaseDeclaration):
    """Simplest BaseDeclaration computed by calling the given function.

    Attributes:
        function (function): a function without arguments and
            returning the computed value.
    """

    def __init__(self, function, *args, **kwargs):
        super(LazyFunction, self).__init__(*args, **kwargs)
        self.function = function

    def evaluate(self, instance, step, extra):
        logger.debug("LazyFunction: Evaluating %s on %s", utils.log_repr(self.function), utils.log_repr(step))
        return self.function()


class LazyAttribute(BaseDeclaration):
    """Specific BaseDeclaration computed using a lambda.

    Attributes:
        function (function): a function, expecting the current LazyStub and
            returning the computed value.
    """

    def __init__(self, function, *args, **kwargs):
        super(LazyAttribute, self).__init__(*args, **kwargs)
        self.function = function

    def evaluate(self, instance, step, extra):
        logger.debug("LazyAttribute: Evaluating %s on %s", utils.log_repr(self.function), utils.log_repr(instance))
        return self.function(instance)


class _UNSPECIFIED(object):
    pass


def deepgetattr(obj, name, default=_UNSPECIFIED):
    """Try to retrieve the given attribute of an object, digging on '.'.

    This is an extended getattr, digging deeper if '.' is found.

    Args:
        obj (object): the object of which an attribute should be read
        name (str): the name of an attribute to look up.
        default (object): the default value to use if the attribute wasn't found

    Returns:
        the attribute pointed to by 'name', splitting on '.'.

    Raises:
        AttributeError: if obj has no 'name' attribute.
    """
    try:
        if '.' in name:
            attr, subname = name.split('.', 1)
            return deepgetattr(getattr(obj, attr), subname, default)
        else:
            return getattr(obj, name)
    except AttributeError:
        if default is _UNSPECIFIED:
            raise
        else:
            return default


class SelfAttribute(BaseDeclaration):
    """Specific BaseDeclaration copying values from other fields.

    If the field name starts with two dots or more, the lookup will be anchored
    in the related 'parent'.

    Attributes:
        depth (int): the number of steps to go up in the containers chain
        attribute_name (str): the name of the attribute to copy.
        default (object): the default value to use if the attribute doesn't
            exist.
    """

    def __init__(self, attribute_name, default=_UNSPECIFIED, *args, **kwargs):
        super(SelfAttribute, self).__init__(*args, **kwargs)
        depth = len(attribute_name) - len(attribute_name.lstrip('.'))
        attribute_name = attribute_name[depth:]

        self.depth = depth
        self.attribute_name = attribute_name
        self.default = default

    def evaluate(self, instance, step, extra):
        if self.depth > 1:
            # Fetching from a parent
            target = step.chain[self.depth - 1]
        else:
            target = instance

        logger.debug("SelfAttribute: Picking attribute %r on %s", self.attribute_name, utils.log_repr(target))
        return deepgetattr(target, self.attribute_name, self.default)

    def __repr__(self):
        return '<%s(%r, default=%r)>' % (
            self.__class__.__name__,
            self.attribute_name,
            self.default,
        )


class Iterator(BaseDeclaration):
    """Fill this value using the values returned by an iterator.

    Warning: the iterator should not end !

    Attributes:
        iterator (iterable): the iterator whose value should be used.
        getter (callable or None): a function to parse returned values
    """

    def __init__(self, iterator, cycle=True, getter=None):
        super(Iterator, self).__init__()
        self.getter = getter
        self.iterator = None

        if cycle:
            self.iterator_builder = lambda: utils.ResetableIterator(itertools.cycle(iterator))
        else:
            self.iterator_builder = lambda: utils.ResetableIterator(iterator)

    def evaluate(self, instance, step, extra):
        # Begin unrolling as late as possible.
        # This helps with ResetableIterator(MyModel.objects.all())
        if self.iterator is None:
            self.iterator = self.iterator_builder()

        logger.debug("Iterator: Fetching next value from %s", utils.log_repr(self.iterator))
        value = next(iter(self.iterator))
        if self.getter is None:
            return value
        return self.getter(value)

    def reset(self):
        """Reset the internal iterator."""
        self.iterator.reset()


class Sequence(BaseDeclaration):
    """Specific BaseDeclaration to use for 'sequenced' fields.

    These fields are typically used to generate increasing unique values.

    Attributes:
        function (function): A function, expecting the current sequence counter
            and returning the computed value.
        type (function): A function converting an integer into the expected kind
            of counter for the 'function' attribute.
    """
    def __init__(self, function, type=int):
        super(Sequence, self).__init__()
        self.function = function
        self.type = type

    def evaluate(self, instance, step, extra):
        logger.debug("Sequence: Computing next value of %r for seq=%s", self.function, step.sequence)
        return self.function(self.type(step.sequence))


class LazyAttributeSequence(Sequence):
    """Composite of a LazyAttribute and a Sequence.

    Attributes:
        function (function): A function, expecting the current LazyStub and the
            current sequence counter.
        type (function): A function converting an integer into the expected kind
            of counter for the 'function' attribute.
    """
    def evaluate(self, instance, step, extra):
        logger.debug(
            "LazyAttributeSequence: Computing next value of %r for seq=%s, obj=%s",
            self.function, step.sequence, utils.log_repr(instance))
        return self.function(instance, self.type(step.sequence))


class ContainerAttribute(BaseDeclaration):
    """Variant of LazyAttribute, also receives the containers of the object.

    Attributes:
        function (function): A function, expecting the current LazyStub and the
            (optional) object having a subfactory containing this attribute.
        strict (bool): Whether evaluating should fail when the containers are
            not passed in (i.e used outside a SubFactory).
    """
    def __init__(self, function, strict=True, *args, **kwargs):
        super(ContainerAttribute, self).__init__(*args, **kwargs)
        self.function = function
        self.strict = strict

    def evaluate(self, instance, step, extra):
        """Evaluate the current ContainerAttribute.

        Args:
            obj (LazyStub): a lazy stub of the object being constructed, if
                needed.
            containers (list of LazyStub): a list of lazy stubs of factories
                being evaluated in a chain, each item being a future field of
                next one.
        """
        # Strip the current instance from the chain
        chain = step.chain[1:]
        if self.strict and not chain:
            raise TypeError(
                "A ContainerAttribute in 'strict' mode can only be used "
                "within a SubFactory.")

        return self.function(instance, chain)


class ParameteredAttribute(BaseDeclaration):
    """Base class for attributes expecting parameters.

    Attributes:
        defaults (dict): Default values for the paramters.
            May be overridden by call-time parameters.

    Class attributes:
        CONTAINERS_FIELD (str): name of the field, if any, where container
            information (e.g for SubFactory) should be stored. If empty,
            containers data isn't merged into generate() parameters.
    """

    CONTAINERS_FIELD = '__containers'

    # Whether to add the current object to the stack of containers
    EXTEND_CONTAINERS = False

    def __init__(self, **kwargs):
        super(ParameteredAttribute, self).__init__()
        self.defaults = kwargs

    def _prepare_containers(self, obj, containers=()):
        if self.EXTEND_CONTAINERS:
            return (obj,) + tuple(containers)

        return containers

    def evaluate(self, instance, step, extra):
        """Evaluate the current definition and fill its attributes.

        Uses attributes definition in the following order:
        - values defined when defining the ParameteredAttribute
        - additional values defined when instantiating the containing factory

        Args:
            instance (builder.Resolver): The object holding currently computed
                attributes
            step: a factory.builder.BuildStep
            extra (dict): additional, call-time added kwargs
                for the step.
        """
        defaults = dict(self.defaults)
        if extra:
            defaults.update(extra)

        return self.generate(step, defaults)

    def generate(self, step, params):
        """Actually generate the related attribute.

        Args:
            sequence (int): the current sequence number
            obj (LazyStub): the object being constructed
            create (bool): whether the calling factory was in 'create' or
                'build' mode
            params (dict): parameters inherited from init and evaluation-time
                overrides.

        Returns:
            Computed value for the current declaration.
        """
        raise NotImplementedError()


class _FactoryWrapper(object):
    """Handle a 'factory' arg.

    Such args can be either a Factory subclass, or a fully qualified import
    path for that subclass (e.g 'myapp.factories.MyFactory').
    """
    def __init__(self, factory_or_path):
        self.factory = None
        self.module = self.name = ''
        if isinstance(factory_or_path, type):
            self.factory = factory_or_path
        else:
            if not (compat.is_string(factory_or_path) and '.' in factory_or_path):
                raise ValueError(
                    "A factory= argument must receive either a class "
                    "or the fully qualified path to a Factory subclass; got "
                    "%r instead." % factory_or_path)
            self.module, self.name = factory_or_path.rsplit('.', 1)

    def get(self):
        if self.factory is None:
            self.factory = utils.import_object(
                self.module,
                self.name,
            )
        return self.factory

    def __repr__(self):
        if self.factory is None:
            return '<_FactoryImport: %s.%s>' % (self.module, self.name)
        else:
            return '<_FactoryImport: %s>' % self.factory.__class__


class SubFactory(ParameteredAttribute):
    """Base class for attributes based upon a sub-factory.

    Attributes:
        defaults (dict): Overrides to the defaults defined in the wrapped
            factory
        factory (base.Factory): the wrapped factory
    """

    EXTEND_CONTAINERS = True
    FORCE_SEQUENCE = False

    def __init__(self, factory, **kwargs):
        super(SubFactory, self).__init__(**kwargs)
        self.factory_wrapper = _FactoryWrapper(factory)

    def get_factory(self):
        """Retrieve the wrapped factory.Factory subclass."""
        return self.factory_wrapper.get()

    def generate(self, step, params):
        """Evaluate the current definition and fill its attributes.

        Args:
            step: a factory.builder.BuildStep
            params (dict): additional, call-time added kwargs
                for the step.
        """
        subfactory = self.get_factory()
        logger.debug(
            "SubFactory: Instantiating %s.%s(%s), create=%r",
            subfactory.__module__, subfactory.__name__,
            utils.log_pprint(kwargs=params),
            step,
        )
        force_sequence = step.sequence if self.FORCE_SEQUENCE else None
        return step.recurse(subfactory, params, force_sequence=force_sequence)


class Dict(SubFactory):
    """Fill a dict with usual declarations."""

    FORCE_SEQUENCE = True

    def __init__(self, params, dict_factory='factory.DictFactory'):
        super(Dict, self).__init__(dict_factory, **dict(params))


class List(SubFactory):
    """Fill a list with standard declarations."""

    FORCE_SEQUENCE = True

    def __init__(self, params, list_factory='factory.ListFactory'):
        params = dict((str(i), v) for i, v in enumerate(params))
        super(List, self).__init__(list_factory, **params)


# Parameters
# ==========


class UNDEFINED(object):
    pass


class Maybe(BaseDeclaration):
    def __init__(self, decider, yes_declaration, no_declaration=None):
        self.decider = decider
        self.yes = yes_declaration
        self.no = no_declaration

    def evaluate(self, instance, step, extra):
        decider = getattr(instance, self.decider, None)
        target = self.yes if decider else self.no

        if isinstance(target, BaseDeclaration):
            return target.evaluate(
                instance=instance,
                step=step,
                extra=extra,
            )
        else:
            # Flat value
            return target

    def __repr__(self):
        return 'Maybe(%r, yes=%r, no=%r)' % (self.decider, self.yes, self.no)


class Parameter(object):
    """A complex parameter, to be used in a Factory.Params section.

    Must implement:
    - A "compute" function, performing the actual declaration override
    - Optionally, a get_revdeps() function (to compute other parameters it may alter)
    """

    def as_declarations(self, field_name, declarations):
        """Compute the overrides for this parameter.

        Args:
        - field_name (str): the field this parameter is installed at
        - declarations (dict): the global factory declarations

        Returns:
            dict: the declarations to override
        """
        raise NotImplementedError()

    def get_revdeps(self, parameters):
        """Retrieve the list of other parameters modified by this one."""
        return []


class SimpleParameter(Parameter):
    def __init__(self, value):
        self.value = value

    def as_declarations(self, field_name, declarations):
        return {
            field_name: self.value,
        }

    @classmethod
    def wrap(cls, value):
        if not isinstance(value, Parameter):
            return cls(value)
        return value


class Trait(Parameter):
    """The simplest complex parameter, it enables a bunch of new declarations based on a boolean flag."""
    def __init__(self, **overrides):
        self.overrides = overrides

    def as_declarations(self, field_name, declarations):
        overrides = {}
        for maybe_field, new_value in self.overrides.items():
            overrides[maybe_field] = Maybe(
                decider=field_name,
                yes_declaration=new_value,
                no_declaration=declarations.get(maybe_field, None),
            )
        return overrides

    def get_revdeps(self, parameters):
        """This might alter fields it's injecting."""
        return [param for param in parameters if param in self.overrides]


# Post-generation
# ===============


class ExtractionContext(object):
    """Private class holding all required context from extraction to postgen."""
    def __init__(self, value=None, did_extract=False, extra=None, for_field=''):
        self.value = value
        self.did_extract = did_extract
        self.extra = extra or {}
        self.for_field = for_field

    def __repr__(self):
        return 'ExtractionContext(%r, %r, %r)' % (
            self.value,
            self.did_extract,
            self.extra,
        )


class PostGenerationDeclaration(object):
    """Declarations to be called once the model object has been generated."""

    creation_counter = 0
    """Global creation counter of the declaration."""

    def __init__(self, *args, **kwargs):
        self.creation_counter = PostGenerationDeclaration.creation_counter
        PostGenerationDeclaration.creation_counter += 1

    def extract(self, name, attrs):
        """Extract relevant attributes from a dict.

        Args:
            name (str): the name at which this PostGenerationDeclaration was
                defined in the declarations
            attrs (dict): the attribute dict from which values should be
                extracted

        Returns:
            (object, dict): a tuple containing the attribute at 'name' (if
                provided) and a dict of extracted attributes
        """
        try:
            extracted = attrs.pop(name)
            did_extract = True
        except KeyError:
            extracted = None
            did_extract = False

        kwargs = utils.extract_dict(name, attrs)
        return ExtractionContext(extracted, did_extract, kwargs, name)

    def call(self, instance, step, context):  # pragma: no cover
        """Call this hook; no return value is expected.

        Args:
            obj (object): the newly generated object
            create (bool): whether the object was 'built' or 'created'
            context: An ExtractionContext containing values
                extracted from the containing factory's declaration
        """
        raise NotImplementedError()


class PostGeneration(PostGenerationDeclaration):
    """Calls a given function once the object has been generated."""
    def __init__(self, function):
        super(PostGeneration, self).__init__()
        self.function = function

    def call(self, instance, step, context):
        logger.debug(
            "PostGeneration: Calling %s.%s(%s)",
            self.function.__module__,
            self.function.__name__,
            utils.log_pprint(
                (instance, step),
                context,
            ),
        )
        return self.function(
            instance, step, context.value, **context.extra)


class RelatedFactory(PostGenerationDeclaration):
    """Calls a factory once the object has been generated.

    Attributes:
        factory (Factory): the factory to call
        defaults (dict): extra declarations for calling the related factory
        name (str): the name to use to refer to the generated object when
            calling the related factory
    """

    def __init__(self, factory, factory_related_name='', **defaults):
        super(RelatedFactory, self).__init__()

        self.name = factory_related_name
        self.defaults = defaults
        self.factory_wrapper = _FactoryWrapper(factory)

    def get_factory(self):
        """Retrieve the wrapped factory.Factory subclass."""
        return self.factory_wrapper.get()

    def call(self, instance, step, context):
        factory = self.get_factory()

        if context.did_extract:
            # The user passed in a custom value
            logger.debug(
                "RelatedFactory: Using provided %s instead of generating %s.%s.",
                utils.log_repr(context.value),
                factory.__module__, factory.__name__,
            )
            return context.value

        passed_kwargs = dict(self.defaults)
        passed_kwargs.update(context.extra)
        if self.name:
            passed_kwargs[self.name] = instance

        logger.debug(
            "RelatedFactory: Generating %s.%s(%s)",
            factory.__module__,
            factory.__name__,
            utils.log_pprint((step,), passed_kwargs),
        )
        return step.recurse(factory, passed_kwargs)


class PostGenerationMethodCall(PostGenerationDeclaration):
    """Calls a method of the generated object.

    Attributes:
        method_name (str): the method to call
        method_args (list): arguments to pass to the method
        method_kwargs (dict): keyword arguments to pass to the method

    Example:
        class UserFactory(factory.Factory):
            ...
            password = factory.PostGenerationMethodCall('set_pass', password='')
    """
    def __init__(self, method_name, *args, **kwargs):
        super(PostGenerationMethodCall, self).__init__()
        self.method_name = method_name
        self.method_args = args
        self.method_kwargs = kwargs

    def call(self, instance, step, context):
        if not context.did_extract:
            passed_args = self.method_args

        elif len(self.method_args) <= 1:
            # Max one argument expected
            passed_args = (context.value,)
        else:
            passed_args = tuple(context.value)

        passed_kwargs = dict(self.method_kwargs)
        passed_kwargs.update(context.extra)
        method = getattr(instance, self.method_name)
        logger.debug(
            "PostGenerationMethodCall: Calling %s.%s(%s)",
            utils.log_repr(instance),
            self.method_name,
            utils.log_pprint(passed_args, passed_kwargs),
        )
        return method(*passed_args, **passed_kwargs)
