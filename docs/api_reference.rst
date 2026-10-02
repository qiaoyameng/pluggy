:orphan:

.. _`api-reference`:

API Reference
=============

.. autoclass:: pluggy.PluginManager
    :members:

.. autoclass:: pluggy.PluginValidationError
    :show-inheritance:
    :members:

.. autodecorator:: pluggy.HookspecMarker

.. autodecorator:: pluggy.HookimplMarker

.. autoclass:: pluggy.HookRelay()
    :members:

    .. data:: <hook name>

        :type: HookCaller

        The caller for the hook with the given name.

.. autoclass:: pluggy.HookCaller()
    :members:
    :special-members: __call__

.. autoclass:: pluggy.HookCallError()
    :show-inheritance:
    :members:

.. autoclass:: pluggy.Result()
    :show-inheritance:
    :members:

.. autoclass:: pluggy.HookImpl()
    :members:

.. autoclass:: pluggy.HookspecOpts()
    :show-inheritance:
    :members:

.. autoclass:: pluggy.HookimplOpts()
    :show-inheritance:
    :members:

Implementation observation
--------------------------

.. autoclass:: pluggy.HookImplObserver()
    :members:

.. autoclass:: pluggy.HookCallEvent()
    :members:

.. autoclass:: pluggy.HookCallResultEvent()
    :members:

.. autoclass:: pluggy.ImplStartEvent()
    :members:

.. autoclass:: pluggy.ImplEndEvent()
    :members:

.. autoclass:: pluggy.ImplExceptionEvent()
    :members:


Warnings
--------

Custom warnings generated in some situations such as improper usage or deprecated features.

.. autoclass:: pluggy.PluggyWarning()
    :show-inheritance:

.. autoclass:: pluggy.PluggyObserverWarning()
    :show-inheritance:

.. autoclass:: pluggy.PluggyTeardownRaisedWarning()
    :show-inheritance:
