"""Smoke tests for ResponsaClient.hard_reset().

Genuinely disruptive: unlike every other test in this suite, this one
actually QUITS the real, possibly-already-running Responsa process (not
just detaches) and relaunches it -- so it's slow (~40s+, Responsa's own
relaunch plus restoring its previous windows) and, unlike close(), does
affect anyone else currently using the app. Not marked @pytest.mark.slow
(that marker is for queries that are merely big, not for tests that quit
the shared app), but be aware before running this file casually.

    pytest tests/test_hard_reset.py -v
"""
QUERY = "ספינה טובעת"


def test_hard_reset_actually_relaunches_the_process(client):
    old_pid = client._impl._pid
    client.hard_reset()
    assert client._impl._pid is not None
    assert client._impl._pid != old_pid, "expected a genuinely new process, not the same one"


def test_client_still_works_after_a_hard_reset(client):
    client.hard_reset()
    results = client.search(QUERY)
    assert results.hits, "expected the fresh instance to search normally"


def test_hard_reset_with_clear_windows_false_does_not_raise(client):
    client.hard_reset(clear_windows=False)
