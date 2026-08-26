import unittest

from security import extract_token, is_valid_token, new_token, share_payload, token_hash


class SecurityTests(unittest.TestCase):
    def test_token_is_valid_and_hash_is_deterministic(self):
        token = new_token()
        self.assertTrue(is_valid_token(token))
        self.assertEqual(token_hash(token), token_hash(token))

    def test_extracts_raw_token_and_telegram_link(self):
        token = new_token()
        self.assertEqual(extract_token(token), token)
        self.assertEqual(extract_token(f"https://t.me/example_bot?start={token}"), token)
        self.assertIsNone(extract_token("ZmFrZS1sZWdhY3ktbGluaw"))

    def test_share_payload_rejects_invalid_token(self):
        with self.assertRaises(ValueError):
            share_payload("not-a-token")


if __name__ == "__main__":
    unittest.main()