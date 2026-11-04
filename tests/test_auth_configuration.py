import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils import auth


class CredentialConfigurationTests(unittest.TestCase):
    def test_placeholders_are_rejected_before_network_request(self):
        with patch.dict(os.environ, {
            "CLIENT_ID": "your_client_id_here",
            "CLIENT_SECRET": "your_client_secret_here",
            "REFRESH_TOKEN": "your_refresh_token_here",
        }, clear=True), patch.object(auth, "load_dotenv"), patch.object(
            auth.requests, "post"
        ) as post:
            with self.assertRaisesRegex(ValueError, "placeholder credentials"):
                auth.get_access_token(force_refresh=True)
            post.assert_not_called()

    def test_missing_values_are_rejected(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(auth, "load_dotenv"):
            with self.assertRaisesRegex(ValueError, "CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN"):
                auth.load_credentials_from_env()

    def test_saved_file_replaces_stale_process_values(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "CLIENT_ID=test-client\nCLIENT_SECRET=test-secret\n"
                "REFRESH_TOKEN=test-refresh\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {
                "CLIENT_ID": "your_client_id_here",
                "CLIENT_SECRET": "your_client_secret_here",
                "REFRESH_TOKEN": "your_refresh_token_here",
            }, clear=True), patch.object(auth, "ENV_FILE", env_file):
                credentials = auth.load_credentials_from_env()
                self.assertEqual(credentials["client_id"], "test-client")
                env_file.write_text(
                    "CLIENT_ID=updated-client\nCLIENT_SECRET=test-secret\n"
                    "REFRESH_TOKEN=test-refresh\n",
                    encoding="utf-8",
                )
                self.assertEqual(
                    auth.load_credentials_from_env()["client_id"], "updated-client"
                )


if __name__ == "__main__":
    unittest.main()
