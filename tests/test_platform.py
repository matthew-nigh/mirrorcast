import mirrorcast
from mirrorcast.platform_utils import FIREWALL_RULE_NAME, firewall_rule_commands


def test_app_identity():
    assert mirrorcast.APP_NAME == "MirrorCast"
    assert mirrorcast.PUBLISHER == "M. Nigh"


def test_firewall_rule_only_opens_stream_port_to_local_network():
    delete, add = firewall_rule_commands(8765)
    assert delete[:4] == ["netsh", "advfirewall", "firewall", "delete"]
    assert f"name={FIREWALL_RULE_NAME}" in delete and f"name={FIREWALL_RULE_NAME}" in add
    for part in ("dir=in", "action=allow", "protocol=TCP", "localport=8765",
                 "remoteip=localsubnet"):
        assert part in add
    assert all(" " not in part for part in add)  # survives cmd.exe quoting
