"""Isolated PDF backend. Passwords arrive over stdin and never enter arguments."""
import json
import sys


def main():
    request = json.loads(sys.stdin.buffer.read(2 * 1024 * 1024).decode("utf-8"))
    try:
        from .pdf_backend import dispatch
        result = dispatch(request)
        print(json.dumps(dict(result=result), ensure_ascii=True), flush=True)
        return 0
    except Exception as error:
        message = str(error)
        for secret in request.get("secrets", {}).values():
            if secret:
                message = message.replace(secret, "[redacted]")
        sys.stderr.write(message[:3000] or type(error).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
