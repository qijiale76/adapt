import numpy as np
import tensorflow as tf
import tensorflow.keras.backend as K

from adapt import Network
from adapt.fuzzer.archive import Archive
from adapt.metric import NeuronCoverage
from adapt.strategy import RandomStrategy
from adapt.utils.functional import coverage
from adapt.utils.timer import Timeout
from adapt.utils.timer import Timer

class WhiteBoxFuzzer:
  '''A white-box fuzzer for deep neural network.
  
  White-box testing is a technique that utilizes internal values to generate
  inputs. This class will uses the gradients to generate next input for testing.
  This fuzzer will test one image.
  '''

  def __init__(self, network, image, metric=None, strategy=None, k=10, delta=0.2, class_weight=0.5, neuron_weight=0.5, lr=0.02, trail=3, decode=None):
    '''Create a fuzzer.
    
    Create a white-box fuzzer. All parameters except for the time budget, should
    be set.

    Args:
      network: A wrapped Keras model with `adapt.Network`. Wrap if not wrapped.
      image: An image to test.
      metric: A coverage metric for testing. By default, the fuzzer will use
        a neuron coverage with threshold 0.5.
      strategy: A neuron selection strategy. By default, the fuzzer will use
        the `adapt.strategy.RandomStrategy`.
      k: A positive integer. The number of the neurons to select.
      delta: A positive floating point number. Limits of distance of created
        inputs.
      class_weight: A floating point number. A weight for the class term in
        optimization equation.
      neuron_weight: A floating point number. A weight for the neuron term in
        optimization equation.
      lr: A floating point number. A learning rate to apply when generating
        the next input using gradients.
      trail: A positive integer. Trails to apply one set of selected neurons.
      decode: A function that gets logits and return the label. By default,
        uses `np.argmax`.

    Raises:
      ValueError: When arguments are not in their proper range.
    '''

    # Store variables.
    if not isinstance(network, Network):
      network = Network(network)
    self.network = network

    self.image = image

    if not metric:
      metric = NeuronCoverage(0.5)
    self.metric = metric

    if not strategy:
      strategy = RandomStrategy(self.network)
    self.strategy = strategy

    if k < 1:
      raise ValueError('The argument k is not positive.')
    self.k = int(k)

    if delta <= 0:
      raise ValueError('The argument delta is not positive.')
    self.delta = float(delta)

    self.class_weight = float(class_weight)
    self.neuron_weight = float(neuron_weight)
    self.lr = float(lr)

    if trail < 1:
      raise ValueError('The argument trails is not positive.')
    self.trail = trail

    if not decode:
      decode = np.argmax
    self.decode = decode

    # Variables that are set during (or after) testing.
    self.archive = None

    self.start_time = None
    self.time_consumed = None

    self.label = None
    self.orig_coverage = None
    self.covered = None
    self.coverage = None
    self.timestamp = None

  def start(self, hours=0, minutes=0, seconds=0, append='meta'):
    '''Start fuzzing for the given time budget.

    Start fuzzing for a time budget.

    Args:
      hours: A non-negative integer which indicates the time budget in hours.
        0 for the default value.
      minutes: A non-negative integer which indicates the time budget in minutes.
        0 for the defalut value.
      seconds: A non-negative integer which indicates the time budget in seconds.
        0 for the defalut value. If hours, minutes, and seconds are set to be 0,
        the time budget will automatically set to be 10 seconds.
      append: An option that specifies the data that archive stores. Should be one
        of "meta" or "all". By default, "meta" will be used.
    '''

    # Get the original properties.
    internals, logits = self.network.predict(np.array([self.image]))
    orig_index = np.argmax(logits)
    orig_norm = np.linalg.norm(self.image)
    self.label = self.decode(np.array([logits]))
    self.orig_coverage = coverage(self.image)

    # Initialize variables.
    self.covered = self.metric(internals=internals, logits=logits)
    self.archive = Archive(self.image, self.label, append=append)
    self.timestamp = []

    # Initialize the strategy.
    self.strategy = self.strategy.init(covered=self.covered, label=self.label)

    # Set timer.
    timer = Timer(hours, minutes, seconds)
    print('Fuzzing started. Press ctrl+c to quit.')

    # Loop until timeout, or interrupted by user.
    try:
      while True:

        # Create worklist.
        worklist = [tf.identity(np.array([self.image]))]

        # While worklist is not empty:
        while len(worklist) > 0:

          # Get image
          image = worklist.pop(0)

          # Select neurons.
          neurons = self.strategy(k=self.k)

          # Try trail times.
          for _ in range(self.trail):

            # Get original coverage
            orig_cov = coverage(self.covered)

            # Calculate gradients.
            with tf.GradientTape() as t:
              t.watch(image)
              internals, logits = self.network.predict(image)
              loss = self.neuron_weight * K.sum([internals[li][ni] for li, ni in neurons]) - self.class_weight * logits[orig_index]
            dl_di = t.gradient(loss, image)

            # Generate the next image using gradients.
            image += self.lr * dl_di

            # Get the properties of the generated image.
            internals, logits = self.network.predict(image)

            covered = self.metric(internals=internals, logits=logits)
            label = self.decode(np.array([logits]))

            distance = np.linalg.norm(image - self.image) / orig_norm

            # Update varaibles in fuzzer
            self.covered = np.bitwise_or(self.covered, covered)

            new_cov = covered(self.covered)

            # If coverage increased.
            if new_cov > orig_cov and distance < self.delta:
              worklist.append(tf.identity(image))

            # Feedback to strategy.
            self.strategy.update(covered=covered, label=label)

            # Add created image.
            self.archive.add(image, label, distance)
            self.timestamp((timer.elapsed.total_seconds(), new_cov))

            # Check timeout
            timer.check_timeout()

    except Timeout:
      pass
    except KeyboardInterrupt:
      print('Stopped by the user.')

    # Update meta variables.
    self.coverage = coverage(self.image)
    self.start_time = timer.start_time
    self.time_consumed = int(timer.elapsed.total_seconds())

    return self.archive