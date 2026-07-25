import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from check_no_secrets import findings  # noqa: E402


def test_secret_scan_allows_environment_placeholders():
    assert findings('"apiKey": "${OPENAI_API_KEY}"') == []
    assert findings("ZHIPU_API_KEY=your-local-key") == []


def test_secret_scan_rejects_high_confidence_tokens_and_assignments():
    google_key = "AI" + "za" + "A" * 35
    assigned_secret = "DEEPSEEK_" + "API_KEY=" + "real-private-value"
    assert findings(google_key)
    assert findings(assigned_secret)
