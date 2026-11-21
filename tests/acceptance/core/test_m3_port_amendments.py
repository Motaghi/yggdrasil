import pytest

from a1_observations import entry


def current_activation(driver):
    node = entry(driver.status(), "nodes", "pm1tl1")
    assert isinstance(node["activation"], dict)
    return node["activation"]


def message_text(call):
    for text in [*call["argv"], call.get("stdin", "")]:
        if text.startswith("[ygg "):
            return text
    return ""


def pm1tl1_t3_starts(driver):
    return [call for call in driver.a5_t3_calls("thread start")
            if "--title" in call["argv"]
            and call["argv"][call["argv"].index("--title") + 1].startswith("pm1tl1 ")]


def pm1tl1_t3_sends(driver):
    return [call for call in driver.a5_t3_calls("thread send")
            if "th-4" in call["argv"]]


def assert_no_t3_or_headless_launch(driver):
    assert pm1tl1_t3_sends(driver) == []
    assert pm1tl1_t3_starts(driver) == []
    assert driver.launches("pm1tl1") == []


def missing_thread(driver):
    driver.m3_t3_seed(owes_message=True)
    activation = current_activation(driver)
    driver.m3_t3_thread(show={"exit": 1, "stdout": "",
                              "stderr": "Thread th-4 is no longer available."})
    driver.clear_calls()
    driver.run("conductor", "tick")
    status = driver.status()
    assert isinstance(status, dict)
    assert activation["id"] in status["uncertain_keys"]
    assert_no_t3_or_headless_launch(driver)
    return activation["id"]


@pytest.mark.ac("M3P-AC-01")
def test_running_t3_thread_holds_owed_message_without_another_launch(tree):
    tree.m3_t3_seed(owes_message=True)
    tree.m3_t3_thread(status="running", active=True)
    tree.clear_calls()
    tree.run("conductor", "tick")
    node = entry(tree.status(), "nodes", "pm1tl1")
    assert node["state"] == "running"
    assert isinstance(node["activation"], dict)
    assert node["activation"]["outcome"] == "confirmed"
    assert_no_t3_or_headless_launch(tree)


@pytest.mark.ac("M3P-AC-01")
def test_failed_t3_run_launches_new_activation_in_the_same_thread(tree):
    tree.m3_t3_seed()
    seed = current_activation(tree)
    tree.m3_t3_thread(status="failed", active=False, last_assistant=(
        "API Error: 400 unknown provider for model not-a-real-model-xyz"
    ))
    tree.clear_calls()
    tree.run("conductor", "tick")
    status = tree.status()
    assert isinstance(status, dict)
    assert seed["id"] not in status["uncertain_keys"]
    activation = current_activation(tree)
    assert activation["id"] != seed["id"]
    sends = pm1tl1_t3_sends(tree)
    assert len(sends) == 1
    assert "th-4" in sends[0]["argv"]
    assert message_text(sends[0]).splitlines()[0] == "[ygg " + activation["id"] + " launch]"
    assert pm1tl1_t3_starts(tree) == []
    assert tree.launches("pm1tl1") == []


@pytest.mark.ac("M3P-AC-01")
def test_unrecognised_t3_status_holds_the_seed_activation(tree):
    tree.m3_t3_seed(owes_message=True)
    seed = current_activation(tree)
    tree.m3_t3_thread(status="interrupted")
    tree.clear_calls()
    tree.run("conductor", "tick")
    status = tree.status()
    assert isinstance(status, dict)
    assert seed["id"] in status["uncertain_keys"]
    assert_no_t3_or_headless_launch(tree)


@pytest.mark.ac("M3P-AC-02")
def test_gone_t3_thread_holds_the_seed_activation_until_release(tree):
    missing_thread(tree)


@pytest.mark.ac("M3P-AC-02")
def test_released_gone_t3_thread_starts_a_new_activation_and_thread(tree):
    seed = missing_thread(tree)
    tree.clear_calls()
    released = tree.run("node", "release", "pm1tl1", "--as", "failed")
    assert released.returncode == 0
    tree.run("conductor", "tick")
    activation = current_activation(tree)
    assert activation["id"] != seed
    starts = pm1tl1_t3_starts(tree)
    assert len(starts) == 1
    argv = starts[0]["argv"]
    assert "--title" in argv
    assert argv[argv.index("--title") + 1] == "pm1tl1 " + activation["id"]
    assert pm1tl1_t3_sends(tree) == []


@pytest.mark.ac("M3P-AC-02")
def test_non_json_t3_show_holds_the_seed_activation(tree):
    tree.m3_t3_seed(owes_message=True)
    seed = current_activation(tree)
    tree.m3_t3_thread(show={
        "exit": 0,
        "stdout": "[22:37:37.795] WARN (#20): Database migration history diverges from this build",
        "stderr": "",
    })
    tree.clear_calls()
    tree.run("conductor", "tick")
    status = tree.status()
    assert isinstance(status, dict)
    assert seed["id"] in status["uncertain_keys"]
    assert_no_t3_or_headless_launch(tree)


@pytest.mark.ac("M3P-AC-03")
def test_t3_start_delivers_role_privately_without_prompt_or_brief_in_argv(tree):
    tree.a5_t3(cli_version="0.1.9", server_version="0.1.9")
    tree.ready()
    tree.run("tracker", "write", "--as", "pm1", "--project", "alpha-p1",
             "--kind", "brief", "--body", "Brief marker zebra-5813: ship saves")
    tree.clear_calls()
    result = tree.run("launch", "pm1tl1")
    assert result.returncode == 0
    starts = pm1tl1_t3_starts(tree)
    assert len(starts) == 1
    call = starts[0]
    assert all("zebra-5813" not in argument and "You are Yggdrasil node" not in argument
               for argument in call["argv"])
    activation = current_activation(tree)
    message = message_text(call)
    assert message.splitlines()[0] == "[ygg " + activation["id"] + " launch]"
    prompt = "You are Yggdrasil node pm1tl1"
    if prompt not in call.get("stdin", ""):
        files = tree.m3_t3_message_files(message, call["cwd"])
        assert len(files) == 1
        assert files[0]["mode"] == 0o600
        assert prompt in files[0]["text"]


@pytest.mark.ac("M3P-AC-03")
def test_t3_send_starts_new_activation_without_message_body_in_argv(tree):
    tree.m3_t3_seed(owes_message=True)
    seed = current_activation(tree)
    tree.clear_calls()
    tree.run("conductor", "tick")
    activation = current_activation(tree)
    assert activation["id"] != seed["id"]
    sends = pm1tl1_t3_sends(tree)
    assert len(sends) == 1
    call = sends[0]
    assert "th-4" in call["argv"]
    assert all("start WP-3" not in argument for argument in call["argv"])
    assert message_text(call).splitlines()[0] == "[ygg " + activation["id"] + " m-0010]"
