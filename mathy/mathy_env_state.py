import json
import random
from collections import namedtuple
from typing import List, NamedTuple, Optional, Tuple

import numpy
import tensorflow as tf

from .core.expressions import (
    MathExpression,
    MathTypeKeys,
    TokenTypeSequenceStart,
    TokenTypeSequenceEnd,
)
from .core.parser import ExpressionParser
from .core.tokenizer import TokenEOF
from .core.tree import STOP
from .features import (
    FEATURE_BWD_VECTORS,
    FEATURE_FWD_VECTORS,
    FEATURE_LAST_BWD_VECTORS,
    FEATURE_LAST_FWD_VECTORS,
    FEATURE_LAST_RULE,
    FEATURE_MOVE_COUNTER,
    FEATURE_MOVE_MASK,
    FEATURE_MOVES_REMAINING,
    FEATURE_NODE_COUNT,
    FEATURE_PROBLEM_TYPE,
    pad_array,
)
from .game_modes import (
    MODE_ARITHMETIC,
    MODE_SIMPLIFY_POLYNOMIAL,
    MODE_SOLVE_FOR_VARIABLE,
)

PLAYER_ID_OFFSET = 0
MOVE_COUNT_OFFSET = 1
GAME_MODE_OFFSET = 2

INPUT_EXAMPLES_FILE_NAME = "examples.jsonl"
TRAINING_SET_FILE_NAME = "training.jsonl"


class MathyEnvTimeStep(NamedTuple):
    """Capture summarized environment state for a previous timestep so the
    agent can use context from its history when making new predictions.
    - "raw" is the input text at the timestep
    - "focus" is the index of the node that is acted on
    - "action" is the action taken
    """

    raw: str
    focus: int
    action: int


class MathAgentState:
    moves_remaining: int
    problem: str
    problem_type: str
    reward: float
    history: List[MathyEnvTimeStep]
    focus_index: int
    last_action: int

    def __init__(
        self,
        moves_remaining,
        problem,
        problem_type,
        reward=0.0,
        history=None,
        focus_index=0,
        last_action=None,
    ):
        self.moves_remaining = moves_remaining
        self.focus_index = focus_index  # TODO: remove this, no longer used
        self.problem = problem
        self.last_action = last_action
        self.reward = reward
        self.problem_type = problem_type
        self.history = (
            history[:] if history is not None else [MathyEnvTimeStep(problem, -1, -1)]
        )

    @classmethod
    def copy(cls, from_state):
        return MathAgentState(
            moves_remaining=from_state.moves_remaining,
            problem=from_state.problem,
            reward=from_state.reward,
            problem_type=from_state.problem_type,
            history=from_state.history,
            focus_index=from_state.focus_index,
            last_action=from_state.last_action,
        )


class MathyEnvState(object):
    """Class for holding environment state and extracting features
    to be passed to the policy/value neural network.

    Mutating operations all return a copy of the environment adapter
    with its own state.

    This allocation strategy requires more memory but removes a class
    of potential issues around unintentional sharing of data and mutation
    by two different sources.
    """

    agent: MathAgentState
    parser: ExpressionParser
    max_moves: int

    def __init__(
        self,
        state: Optional["MathyEnvState"] = None,
        problem: str = None,
        max_moves: int = 10,
        problem_type: str = "mathy.unknown",
    ):
        self.parser = ExpressionParser()
        self.max_moves = max_moves
        if problem is not None:
            self.agent = MathAgentState(max_moves, problem, problem_type)
        elif state is not None:
            self.max_moves = state.max_moves
            self.agent = MathAgentState.copy(state.agent)
        else:
            raise ValueError("either state or a problem must be provided")
        # Ensure a string made it into the problem slot
        assert isinstance(self.agent.problem, str)

    @classmethod
    def copy(cls, from_state):
        return MathyEnvState(state=from_state)

    def clone(self):
        return MathyEnvState(state=self)

    def encode_player(
        self, problem: str, action: int, focus_index: int, moves_remaining: int
    ):
        """Encode a player's state into the env_state, and return the env_state"""
        out_state = MathyEnvState.copy(self)
        agent = out_state.agent
        agent.history.append(MathyEnvTimeStep(problem, focus_index, action))
        agent.problem = problem
        agent.action = action
        agent.moves_remaining = moves_remaining
        agent.focus_index = focus_index
        return out_state

    def to_empty_state(self):
        pad_value = MathTypeKeys["empty"]
        return {
            FEATURE_MOVE_COUNTER: [-1],
            FEATURE_LAST_RULE: [-1],
            FEATURE_NODE_COUNT: [-1],
            FEATURE_PROBLEM_TYPE: [self.agent.problem_type],
            FEATURE_FWD_VECTORS: [[[pad_value]]],
            FEATURE_BWD_VECTORS: [[[pad_value]]],
            FEATURE_MOVE_MASK: [[0, 0, 0, 0, 0, 0]],
        }

    def to_empty_window(self):
        return {
            FEATURE_MOVE_COUNTER: [[-1], [-1], [-1]],
            FEATURE_LAST_RULE: [[-1], [-1], [-1]],
            FEATURE_NODE_COUNT: [[-1], [-1], [-1]],
            FEATURE_PROBLEM_TYPE: [
                self.agent.problem_type,
                self.agent.problem_type,
                self.agent.problem_type,
            ],
            FEATURE_FWD_VECTORS: [
                [MathTypeKeys["empty"]],
                [MathTypeKeys["empty"]],
                [MathTypeKeys["empty"]],
            ],
            FEATURE_BWD_VECTORS: [
                [MathTypeKeys["empty"]],
                [MathTypeKeys["empty"]],
                [MathTypeKeys["empty"]],
            ],
            FEATURE_MOVE_MASK: [
                [0, 0, 0, 0, 0, 0],
                [0, 0, 0, 0, 0, 0],
                [0, 0, 0, 0, 0, 0],
            ],
        }

    def to_input_features(self, move_mask, return_batch=False):
        """Output Dict of integer features that can be fed to the
        neural network for prediction.

        When return_batch is true, the outputs will have an array of features for each
        so the result can be directly passed to the predictor.
        """
        expression = self.parser.parse(self.agent.problem)
        # Generate context vectors for the current state's expression tree
        vectors = [[t.type_id] for t in expression.toList()]
        # # Start each sequence with the start token
        # vectors.insert(0, [TokenTypeSequenceStart])
        # # End it with a specific end token
        # vectors.append([TokenTypeSequenceEnd])

        # Encode the last action
        last_action = -1
        if len(self.agent.history) >= 1:
            last_action = self.agent.history[-1].action

        # After padding is done, generate reversed vectors for the bilstm
        vectors_reversed = vectors[:]
        vectors_reversed.reverse()

        def maybe_wrap(value):
            nonlocal return_batch
            return [value] if return_batch else value

        return {
            FEATURE_MOVES_REMAINING: maybe_wrap(self.agent.moves_remaining),
            FEATURE_MOVE_COUNTER: maybe_wrap(
                int(self.max_moves - self.agent.moves_remaining)
            ),
            FEATURE_LAST_RULE: maybe_wrap(last_action),
            FEATURE_NODE_COUNT: maybe_wrap(len(expression.toList())),
            FEATURE_PROBLEM_TYPE: maybe_wrap(self.agent.problem_type),
            FEATURE_FWD_VECTORS: maybe_wrap(vectors),
            FEATURE_BWD_VECTORS: maybe_wrap(vectors_reversed),
            FEATURE_MOVE_MASK: maybe_wrap(move_mask),
        }
