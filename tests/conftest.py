import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

SA_EMAIL = "crashdigest@example-prod.iam.gserviceaccount.com"


@pytest.fixture(scope="session")
def private_key_pem():
    """Ключ генерируется на лету: настоящий PEM в репозитории сканеры
    секретов приняли бы за утечку, даже если он ничего не открывает.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


@pytest.fixture
def service_account_key(tmp_path, private_key_pem):
    """Путь к JSON-ключу сервисного аккаунта в формате Google Cloud."""
    path = tmp_path / "service-account.json"
    path.write_text(json.dumps({
        "type": "service_account",
        "project_id": "example-prod",
        "private_key_id": "0" * 40,
        "private_key": private_key_pem,
        "client_email": SA_EMAIL,
        "client_id": "100000000000000000000",
        "token_uri": "https://oauth2.googleapis.com/token",
    }))
    return str(path)
