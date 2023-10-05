# Copyright (c) 2023 Rocket Science AG, Switzerland

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from typing import Iterable

import aioresponses
import numpy as np
import pytest
import requests_mock as rmock
from pytest_mock import MockerFixture

from rrosti.chat import chat_session
from rrosti.chat.state_machine import execution, interpolable, parsing
from rrosti.llm_api import openai_api, openai_api_direct
from rrosti.servers.websocket_query_server import Frontend
from rrosti.snippets.abstract_snippet_database import AbstractSnippetDatabase
from rrosti.snippets.snippet import Snippet


def make_state_machine_runner(mocker: MockerFixture, code: str) -> execution.StateMachineRunner:
    openai_provider = openai_api_direct.DirectOpenAIApiProvider()
    llm: chat_session.LLM = mocker.Mock(spec=chat_session.LLM, openai_provider=openai_provider)
    llm.chat_completion.return_value = chat_session.Message(  # type: ignore[attr-defined]
        role="assistant",
        importance=chat_session.Importance.LOW,
        ttl=None,
        text="Hello, world from chat completion!",
    )

    frontend: Frontend = mocker.Mock(spec=Frontend)
    sm = parsing.loads_from_yaml(code)
    return execution.StateMachineRunner(sm=sm, llm=llm, frontend=frontend, openai_provider=openai_provider)


@pytest.fixture(autouse=True)
def _no_network(requests_mock: rmock.Mocker) -> Iterable[None]:
    with aioresponses.aioresponses():
        yield


async def test_execution(mocker: MockerFixture) -> None:
    runner = make_state_machine_runner(
        mocker,
        r"""
    agents:
    -   name: agent1
        states:
        -   name: initial
            action:
            -   goto: some_state
        -   name: some_state
            conditions:
            -   default:
                    action:
                    -   end
        """,
    )

    await runner.run()


SIMPLE_PYTHON_YAML = r"""
agents:
-   name: agent1
    states:
    -   name: initial
        action:
        -   goto: some_state
    -   name: some_state
        conditions:
        -   default:
                action:
                -   message: "The Python output is:\n\n{python()}\n\nEnd of Python output."
                -   end
    """


async def test_python(mocker: MockerFixture) -> None:
    runner = make_state_machine_runner(mocker, SIMPLE_PYTHON_YAML)

    # The LLM gives us a message with a Python block.
    runner._llm.chat_completion.return_value = chat_session.Message(  # type: ignore[attr-defined]
        role="assistant",
        importance=chat_session.Importance.LOW,
        ttl=1,
        text=f"""
Let's execute some Python.

$$$python
print(f"Hello, world! 1+1={1+1}")
$$$

How does that look?
        """.strip(),
    )

    await runner.run()

    # The runner should have appended a message with the Python output.
    msg = runner._agent_runners[0]._session.messages[-1]
    assert msg.role == "user"
    assert msg.text == "The Python output is:\n\nHello, world! 1+1=2\n\nEnd of Python output."


def assert_no_python_block_found_error(runner: execution.StateMachineRunner) -> None:
    assert len(runner._agent_runners[0]._session.messages) == 2
    msg = runner._agent_runners[0]._session.messages[-1].text
    assert msg.startswith("The Python output is:\n\n")
    assert msg.endswith("\n\nEnd of Python output.")
    msg = msg.removeprefix("The Python output is:\n\n").removesuffix("\n\nEnd of Python output.")
    assert msg.startswith("Your message did not contain a Python code block.")


async def test_no_python_block(mocker: MockerFixture) -> None:
    runner = make_state_machine_runner(mocker, SIMPLE_PYTHON_YAML)

    # The LLM gives us a message with a Python block.
    runner._llm.chat_completion.return_value = chat_session.Message(  # type: ignore[attr-defined]
        role="assistant",
        ttl=None,
        importance=chat_session.Importance.LOW,
        text="""
Let's execute some Python.

How does that look?
        """.strip(),
    )

    await runner.run()
    assert_no_python_block_found_error(runner)


async def test_unterminated_python_block(mocker: MockerFixture) -> None:
    runner = make_state_machine_runner(mocker, SIMPLE_PYTHON_YAML)

    # The LLM gives us a message with a Python block.
    runner._llm.chat_completion.return_value = chat_session.Message(  # type: ignore[attr-defined]
        role="assistant",
        importance=chat_session.Importance.LOW,
        ttl=1,
        text="""
Let's execute some Python.

$$$python
print("foo")

How does that look?
        """.strip(),
    )

    await runner.run()
    assert_no_python_block_found_error(runner)


SIMPLE_RTFM_YAML = r"""
agents:
-   name: agent1
    states:
    -   name: initial
        action:
        -   system_message: "Here are your instructions."
        -   goto: some_state
    -   name: some_state
        conditions:
        -   default:
                action:
                -   message: "The RTFM output is:\n\n{rtfm()}\n\nEnd of RTFM output."
                -   end
    """


@pytest.fixture
def snippet_db(mocker: MockerFixture) -> AbstractSnippetDatabase:
    mocker.patch("rrosti.chat.state_machine.interpolable._get_database")
    db = mocker.Mock(spec=AbstractSnippetDatabase)
    db.find_nearest_merged.return_value = [
        Snippet("hello, world", source_filename="source1", start_offset=0, page_start=1, page_end=1),
        Snippet("goodbye, cruel world", source_filename="source2", start_offset=10, page_start=10, page_end=10),
    ]

    interpolable._get_database.return_value = db  # type: ignore[attr-defined]
    return db  # type: ignore[no-any-return]


def assert_rtfm_output_is(runner: execution.StateMachineRunner, expected: str) -> None:
    assert len(runner._agent_runners[0]._session.messages) == 3
    msg = runner._agent_runners[0]._session.messages[-1].text
    assert msg.startswith("The RTFM output is:\n\n")
    assert msg.endswith("\n\nEnd of RTFM output.")
    msg = msg.removeprefix("The RTFM output is:\n\n").removesuffix("\n\nEnd of RTFM output.")
    assert msg == expected


async def mock_query_embedding_async(
    openai_provider: openai_api.OpenAIApiProvider, snippets: list[str]
) -> openai_api.EmbeddingResponse:
    return openai_api.EmbeddingResponse(
        snippets=snippets[:],
        embeddings=np.ones((len(snippets), 123), dtype=np.float32),
        model="fake_model",
        prompt_tokens=42,
    )


async def test_rtfm_stub(mocker: MockerFixture, snippet_db: AbstractSnippetDatabase) -> None:
    runner = make_state_machine_runner(mocker, SIMPLE_RTFM_YAML)

    # The LLM gives us a message with an rtfm block
    runner._llm.chat_completion.return_value = chat_session.Message(  # type: ignore[attr-defined]
        role="assistant",
        ttl=1,
        importance=chat_session.Importance.LOW,
        text="""
Let's execute some RTFM.

$$$rtfm
Something here.
$$$

How does that look?
        """.strip(),
    )

    EXPECTED_OUTPUT = """
Extract #123:

hello, world
-----
Extract #124:

goodbye, cruel world
""".strip()

    mocker.patch.object(openai_api_direct.DirectOpenAIApiProvider, "acreate_embedding", new=mock_query_embedding_async)
    runner.frontend.handle_rtfm_output.return_value = 123  # type: ignore[attr-defined]

    await runner.run()

    assert_rtfm_output_is(runner, EXPECTED_OUTPUT)
