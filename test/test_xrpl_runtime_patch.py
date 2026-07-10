import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PATCH_PATH = ROOT / "docker" / "xrpl-runtime-patch.py"
SPEC = importlib.util.spec_from_file_location("xrpl_runtime_patch", PATCH_PATH)
assert SPEC is not None and SPEC.loader is not None
PATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PATCH)


def test_xrpl_runtime_patch_uses_compressed_public_key_without_legacy_prefix() -> None:
    source = (
        "                if account_type == 'secp256k1':\n"
        "                    verifying_key = private_key_obj.get_verifying_key()\n"
        "                    public_key_bytes = verifying_key.to_string()\n"
        '                    public_key = "00" + public_key_bytes.hex().upper()\n'
    )

    patched = PATCH.patch_secp256k1_pubkey(source)

    assert 'verifying_key.to_string("compressed")' in patched
    assert "public_key = public_key_bytes.hex().upper()" in patched
    assert 'public_key = "00" +' not in patched
    assert PATCH.patch_secp256k1_pubkey(patched) == patched
