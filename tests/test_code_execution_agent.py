import pytest
from src.services.code_execution_agent import CodeExecutionAgent


def test_sandbox_success():
    agent = CodeExecutionAgent()
    code = "print(21 + 21)"
    result = agent.execute_python(code, timeout=15.0)

    assert result.exit_code == 0
    assert result.stdout.strip() == "42"
    assert not result.timed_out


def test_sandbox_timeout():
    agent = CodeExecutionAgent()
    # Code with an infinite loop
    code = "import time\nwhile True:\n    time.sleep(0.1)"
    result = agent.execute_python(code, timeout=2.0)

    assert result.timed_out
    assert result.exit_code == -1
    assert "timed out" in result.error_message.lower()


def test_sandbox_no_network():
    agent = CodeExecutionAgent()
    # Verifies network isolation by checking that only the loopback interface 'lo' is present
    code = (
        "import socket\n"
        "interfaces = [x[1] for x in socket.if_nameindex()]\n"
        "print(interfaces)\n"
    )
    result = agent.execute_python(code, timeout=30.0)

    assert result.exit_code == 0
    # Only loopback should be present
    assert "lo" in result.stdout
    assert "eth0" not in result.stdout
