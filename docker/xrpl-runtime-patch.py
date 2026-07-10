from pathlib import Path


XRPL_AUTH = Path(
    "/opt/conda/envs/hummingbot-api/lib/python3.12/site-packages/"
    "hummingbot/connector/exchange/xrpl/xrpl_auth.py"
)


def patch_secp256k1_pubkey(source: str) -> str:
    old = (
        '                    public_key_bytes = verifying_key.to_string()\n'
        '                    public_key = "00" + public_key_bytes.hex().upper()'
    )
    new = (
        '                    public_key_bytes = verifying_key.to_string("compressed")\n'
        "                    public_key = public_key_bytes.hex().upper()"
    )
    if old in source:
        return source.replace(old, new, 1)
    if new in source:
        return source
    raise RuntimeError("Expected XRPL secp256k1 public-key serialization was not found")


def main() -> None:
    source = XRPL_AUTH.read_text()
    XRPL_AUTH.write_text(patch_secp256k1_pubkey(source))


if __name__ == "__main__":
    main()
