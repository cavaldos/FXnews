#!/usr/bin/env python3
# pyright: reportMissingImports=false
import json
import sys
from pathlib import Path

import browser_cookie3


def main() -> int:
    output_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("tmp/notion-brave-cookies.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cookies = []
    for cookie in browser_cookie3.brave(domain_name="notion.so"):
        cookies.append(
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path or "/",
                "secure": bool(cookie.secure),
                "httpOnly": "httponly" in cookie._rest or "HttpOnly" in cookie._rest,
                "expires": cookie.expires if cookie.expires else -1,
            }
        )

    output_path.write_text(json.dumps(cookies))
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
