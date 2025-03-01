.. currentmodule:: tensordict.nn

tensordict.nn package
=====================

The tensordict.nn package makes it possible to flexibly use TensorDict within
ML pipelines.

Since TensorDict turns parts of one's code to a key-based structure, it is now
possible to build complex graph structures using these keys as hooks.
The basic building block is :class:`~.TensorDictModule`, which wraps an :class:`torch.nn.Module`
instance with a list of input and output keys:

.. code-block::

  >>> from torch.nn import Transformer
  >>> from tensordict import TensorDict
  >>> from tensordict.nn import TensorDictModule
  >>> import torch
  >>> module = TensorDictModule(Transformer(), in_keys=["feature", "target"], out_keys=["prediction"])
  >>> data = TensorDict({"feature": torch.randn(10, 11, 512), "target": torch.randn(10, 11, 512)}, [10, 11])
  >>> data = module(data)
  >>> print(data)
  TensorDict(
      fields={
          feature: Tensor(torch.Size([10, 11, 512]), dtype=torch.float32),
          prediction: Tensor(torch.Size([10, 11, 512]), dtype=torch.float32),
          target: Tensor(torch.Size([10, 11, 512]), dtype=torch.float32)},
      batch_size=torch.Size([10, 11]),
      device=None,
      is_shared=False)

One does not necessarily need to use :class:`~.TensorDictModule`, a custom :class:`torch.nn.Module`
with an ordered list of input and output keys (named :obj:`module.in_keys` and
:obj:`module.out_keys`) will suffice.

A key pain-point of multiple PyTorch users is the inability of nn.Sequential to
handle modules with multiple inputs. Working with key-based graphs can easily
solve that problem as each node in the sequence knows what data needs to be
read and where to write it.

For this purpose, we provide the TensorDictSequential class which passes data
through a sequence of TensorDictModules. Each module in the sequence takes its
input from, and writes its output to the original TensorDict, meaning it's possible
for modules in the sequence to ignore output from their predecessors, or take
additional input from the tensordict as necessary. Here's an example:

.. code-block::

  >>> from tensordict.nn import TensorDictSequential
  >>> class Net(nn.Module):
  ...     def __init__(self, input_size=100, hidden_size=50, output_size=10):
  ...         super().__init__()
  ...         self.fc1 = nn.Linear(input_size, hidden_size)
  ...         self.fc2 = nn.Linear(hidden_size, output_size)
  ...
  ...     def forward(self, x):
  ...         x = torch.relu(self.fc1(x))
  ...         return self.fc2(x)
  ...
  >>> class Masker(nn.Module):
  ...     def forward(self, x, mask):
  ...         return torch.softmax(x * mask, dim=1)
  ...
  >>> net = TensorDictModule(
  ...     Net(), in_keys=[("input", "x")], out_keys=[("intermediate", "x")]
  ... )
  >>> masker = TensorDictModule(
  ...     Masker(),
  ...     in_keys=[("intermediate", "x"), ("input", "mask")],
  ...     out_keys=[("output", "probabilities")],
  ... )
  >>> module = TensorDictSequential(net, masker)
  >>>
  >>> td = TensorDict(
  ...     {
  ...         "input": TensorDict(
  ...             {"x": torch.rand(32, 100), "mask": torch.randint(2, size=(32, 10))},
  ...             batch_size=[32],
  ...         )
  ...     },
  ...     batch_size=[32],
  ... )
  >>> td = module(td)
  >>> print(td)
  TensorDict(
      fields={
          input: TensorDict(
              fields={
                  mask: Tensor(torch.Size([32, 10]), dtype=torch.int64),
                  x: Tensor(torch.Size([32, 100]), dtype=torch.float32)},
              batch_size=torch.Size([32]),
              device=None,
              is_shared=False),
          intermediate: TensorDict(
              fields={
                  x: Tensor(torch.Size([32, 10]), dtype=torch.float32)},
              batch_size=torch.Size([32]),
              device=None,
              is_shared=False),
          output: TensorDict(
              fields={
                  probabilities: Tensor(torch.Size([32, 10]), dtype=torch.float32)},
              batch_size=torch.Size([32]),
              device=None,
              is_shared=False)},
      batch_size=torch.Size([32]),
      device=None,
      is_shared=False)

We can also select sub-graphs easily through the :meth:`~.TensorDictSequential.select_subsequence` method:

.. code-block::

  >>> sub_module = module.select_subsequence(out_keys=[("intermediate", "x")])
  >>> td = TensorDict(
  ...     {
  ...         "input": TensorDict(
  ...             {"x": torch.rand(32, 100), "mask": torch.randint(2, size=(32, 10))},
  ...             batch_size=[32],
  ...         )
  ...     },
  ...     batch_size=[32],
  ... )
  >>> sub_module(td)
  >>> print(td)  # the "output" has not been computed
  TensorDict(
      fields={
          input: TensorDict(
              fields={
                  mask: Tensor(torch.Size([32, 10]), dtype=torch.int64),
                  x: Tensor(torch.Size([32, 100]), dtype=torch.float32)},
              batch_size=torch.Size([32]),
              device=None,
              is_shared=False),
          intermediate: TensorDict(
              fields={
                  x: Tensor(torch.Size([32, 10]), dtype=torch.float32)},
              batch_size=torch.Size([32]),
              device=None,
              is_shared=False)},
      batch_size=torch.Size([32]),
      device=None,
      is_shared=False)

Finally, :mod:`tensordict.nn` comes with a :class:`~.ProbabilisticTensorDictModule` that allows
to build distributions from network outputs and get summary statistics or samples from it
(along with the distribution parameters):

.. code-block::

  >>> import torch
  >>> from tensordict import TensorDict
  >>> from tensordict.nn import TensorDictModule
  >>> from tensordict.nn.distributions import NormalParamWrapper
  >>> from tensordict.nn.functional_modules import make_functional
  >>> from tensordict.nn.prototype import (
  ...     ProbabilisticTensorDictModule,
  ...     ProbabilisticTensorDictSequential,
  ... )
  >>> from torch.distributions import Normal
  >>> td = TensorDict(
  ...     {"input": torch.randn(3, 4), "hidden": torch.randn(3, 8)}, [3]
  ... )
  >>> net = torch.nn.GRUCell(4, 8)
  >>> module = TensorDictModule(
  ...     NormalParamWrapper(net), in_keys=["input", "hidden"], out_keys=["loc", "scale"]
  ... )
  >>> prob_module = ProbabilisticTensorDictModule(
  ...     in_keys=["loc", "scale"],
  ...     out_keys=["sample"],
  ...     distribution_class=Normal,
  ...     return_log_prob=True,
  ... )
  >>> td_module = ProbabilisticTensorDictSequential(module, prob_module)
  >>> td_module(td)
  >>> print(td)
  TensorDict(
      fields={
          action: Tensor(torch.Size([3, 4]), dtype=torch.float32),
          hidden: Tensor(torch.Size([3, 8]), dtype=torch.float32),
          input: Tensor(torch.Size([3, 4]), dtype=torch.float32),
          loc: Tensor(torch.Size([3, 4]), dtype=torch.float32),
          sample_log_prob: Tensor(torch.Size([3, 4]), dtype=torch.float32),
          scale: Tensor(torch.Size([3, 4]), dtype=torch.float32)},
      batch_size=torch.Size([3]),
      device=None,
      is_shared=False)


.. autosummary::
    :toctree: generated/
    :template: td_template_noinherit.rst

    TensorDictModuleBase
    TensorDictModule
    ProbabilisticTensorDictModule
    TensorDictSequential
    TensorDictModuleWrapper

Functional
----------

The tensordict package is compatible with most functorch capabilities.
We also provide a dedicated functional API that leverages the advantages of
tensordict to handle parameters in functional programs.

The :func:`~.make_functional` method will turn a module in a functional module. The
module will be modified in-place and a :class:`tensordict.TensorDict` containing the module
parameters will be returned. This tensordict has a structure that reflects exactly
the structure of the model. In the following example, we show that

1. :func:`~.make_functional` extracts the parameters of the module;

2. These parameters have a structure that matches exactly the structure of the
   model (though they can be flattened using ``params.flatten_keys(".")``).

3. It converts the module and all its sub-modules to be functional.

.. code-block::

  >>> from torch import nn
  >>> from tensordict import TensorDict
  >>> from tensordict.nn import make_functional
  >>> import torch
  >>> from torch import vmap
  >>> layer1 = nn.Linear(3, 4)
  >>> layer2 = nn.Linear(4, 4)
  >>> model = nn.Sequential(layer1, layer2)
  >>> params = make_functional(model)
  >>> x = torch.randn(10, 3)
  >>> out = model(x, params=params)  # params is the last arg (or kwarg)
  >>> intermediate = model[0](x, params["0"])
  >>> out2 = model[1](intermediate, params["1"])
  >>> torch.testing.assert_close(out, out2)

Alternatively, parameters can also be constructed using the following methods:

.. code-block::

  >>> params = TensorDict({name: param for name, param in model.named_parameters()}, []).unflatten_keys(".")
  >>> params = TensorDict(model.state_dict(), [])  # provided that the state_dict() just returns params and buffer tensors

Unlike what is done with functorch, :func:`~.make_functional` does not
distinguish on a high level parameters and buffers (they are all packed together).

.. note::
  Tensordict funcitonal modules can be used in several ways, with parameters
  passed as arguments or keyword arguments.

    >>> params = make_functional(model)
    >>> model(input_td, params)
    >>> # alternatively
    >>> model(input_td, params=params)

  However, this will currently not work:

    >>> get_functional(model)
    >>> model(input_td, params)  # breaks!
    >>> model(input_td, params=params)  # works

  as :func:`get_functional` re-populates
  the module with its parameters, we rely on the keyword argument ``"params"``
  as a signature for a functional call.

.. autosummary::
    :toctree: generated/
    :template: rl_template_noinherit.rst

    get_functional
    is_functional
    make_functional
    repopulate_module

Ensembles
---------
The functional approach enables a straightforward ensemble implementation. 
We can duplicate and reinitialize model copies using the :class:`tensordict.nn.EnsembleModule`

.. code-block::

    >>> import torch
    >>> from torch import nn
    >>> from tensordict.nn import TensorDictModule
    >>> from torchrl.modules import EnsembleModule
    >>> from tensordict import TensorDict
    >>> net = nn.Sequential(nn.Linear(4, 32), nn.ReLU(), nn.Linear(32, 2))
    >>> mod = TensorDictModule(net, in_keys=['a'], out_keys=['b'])
    >>> ensemble = EnsembleModule(mod, num_copies=3)
    >>> data = TensorDict({'a': torch.randn(10, 4)}, batch_size=[10])
    >>> ensemble(data)
    TensorDict(
        fields={
            a: Tensor(shape=torch.Size([3, 10, 4]), device=cpu, dtype=torch.float32, is_shared=False),
            b: Tensor(shape=torch.Size([3, 10, 2]), device=cpu, dtype=torch.float32, is_shared=False)},
        batch_size=torch.Size([3, 10]),
        device=None,
        is_shared=False)

.. autosummary::
    :toctree: generated/
    :template: rl_template_noinherit.rst

    EnsembleModule

Tracing and compiling
---------------------

.. currentmodule:: tensordict.prototype

:class:`~.TensorDictModule` can be compiled using :func:`torch.compile` if it is
first traced using :func:`~.symbolic_trace`.

.. autosummary::
    :toctree: generated/
    :template: rl_template_noinherit.rst

    symbolic_trace

Distributions
-------------

.. py:currentmodule::tensordict.nn.distributions

.. autosummary::
    :toctree: generated/
    :template: rl_template_noinherit.rst

    NormalParamsExtractor
    AddStateIndependentNormalScale
    CompositeDistribution
    Delta
    OneHotCategorical
    TruncatedNormal


Utils
-----

.. currentmodule:: tensordict.nn

.. autosummary::
    :toctree: generated/
    :template: rl_template_noinherit.rst

    make_tensordict
    dispatch
    set_interaction_type
    inv_softplus
    biased_softplus
    set_skip_existing
    skip_existing
    TensorDictParams
