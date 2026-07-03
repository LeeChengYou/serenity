import re
import shlex
from pathlib import Path

def parse_curl_test(path: Path):
    text = path.read_text(encoding="utf-8", errors="replace")
    
    # Simply remove all carets from the Windows cmd cURL command
    text = text.replace('^', '')
    
    print("Normalised string preview:")
    print(text[:400])
    print("="*40)
    
    args = [arg for arg in shlex.split(text, posix=True) if arg.strip()]
    print(f"Parsed {len(args)} arguments successfully!")
    print("First 5 args:", args[:5])
    return args

if __name__ == "__main__":
    parse_curl_test(Path("x_curl/UserTweets.curl"))
