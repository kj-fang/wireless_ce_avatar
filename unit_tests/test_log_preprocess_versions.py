"""
preprocess_log_for_llm redacts IP addresses but must leave dotted-quad
versions alone: the skill rules compare driver versions against fix versions.
"""
import unittest

from utils.log_parser_preprocess import preprocess_log_for_llm


def _one(line):
    return preprocess_log_for_llm([line])[0]


class TestVersionSurvivesIpRedaction(unittest.TestCase):

    def test_driver_banner_version_is_kept(self):
        out = _one("16:00:38:676\t[MINIPORT ][10] [B] Intel WLAN 23.40.0.4, "
                   "obj_rel_winAx64_logs, drvKeyId:9eab89d4, FW SHA1 (INTERNAL): d24e06ed")
        self.assertIn("Intel WLAN 23.40.0.4,", out)
        self.assertNotIn("<IP_ADDR>", out)

    def test_driver_banner_is_matched_whatever_its_case_or_spacing(self):
        self.assertIn("23.0.6.4", _one("[MINIPORT] intel  wlan 23.0.6.4, obj_rel_winTx64_logs"))

    def test_firmware_version_is_still_kept(self):
        self.assertIn("77.12.34.56", _one("[LCM] firmware 77.12.34.56 loaded"))

    def test_ip_address_is_still_redacted(self):
        out = _one("[DHCP] lease 192.168.1.23 from server 192.168.1.1")
        self.assertEqual(out.count("<IP_ADDR>"), 2)
        self.assertNotIn("192.168", out)

    def test_ip_on_a_wlan_line_without_the_banner_is_redacted(self):
        out = _one("[NET] WLAN adapter address 10.0.0.7 on Intel hardware")
        self.assertIn("<IP_ADDR>", out)
        self.assertNotIn("10.0.0.7", out)

    def test_ip_after_the_banner_version_is_redacted(self):
        out = _one("Intel WLAN 23.40.0.4 gateway 10.0.0.1")
        self.assertIn("23.40.0.4", out)
        self.assertNotIn("10.0.0.1", out)


if __name__ == "__main__":
    unittest.main()
