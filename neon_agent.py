import asyncio, json, re, math
from urllib.parse import quote
import websockets
import requests

WS_URL = "wss://neonhealth.software/agent-puzzle/challenge"
NEON_CODE = "9260aa3a3a9cfe0a"

with open("resume_public.txt", "r", encoding="utf-8") as f:
    RESUME_TEXT = f.read()

STATE = {"spoken_history": []}
WIKI_CACHE = {}

def reconstruct_message(fragments):
    frags = sorted(fragments, key=lambda f: f.get("timestamp", 0))
    words = [str(f.get("word", "")).strip() for f in frags]
    words = [w for w in words if w]
    return " ".join(words)

def extract_length_constraints(prompt: str):
    exact = re.search(r"exactly\s+(\d+)\s+characters", prompt, re.I)
    between = re.search(r"between\s+(\d+)\s+and\s+(\d+)\s+characters", prompt, re.I)
    exact_n = int(exact.group(1)) if exact else None
    between_xy = (int(between.group(1)), int(between.group(2))) if between else None
    return exact_n, between_xy

def enforce_text_length(text: str, exact_n, between_xy):
    text = text[:256]
    if exact_n is not None:
        if len(text) > exact_n:
            return text[:exact_n]
        return text + (" " * (exact_n - len(text)))
    if between_xy is not None:
        lo, hi = between_xy
        if len(text) < lo:
            text = text + (" " * (lo - len(text)))
        if len(text) > hi:
            text = text[:hi]
    return text

def wants_pound(prompt: str) -> bool:
    low = prompt.lower()
    return ("pound key" in low) or ("followed by the pound" in low) or ("followed by #" in low) or ("#" in prompt)

# JS remainder: a % b = a - trunc(a/b)*b
def js_remainder(a, b):
    if b == 0:
        return float("nan")
    return a - math.trunc(a / b) * b

def tokenize_expr(s: str):
    token_spec = [
        ("NUMBER", r"\d+(\.\d+)?"),
        ("FLOOR",  r"\bfloor\b"),
        ("OP",     r"[+\-*/%]"),
        ("LP",     r"\("),
        ("RP",     r"\)"),
        ("COMMA",  r","),
        ("WS",     r"\s+"),
    ]
    tok_re = re.compile("|".join(f"(?P<{n}>{p})" for n,p in token_spec))
    out = []
    for m in tok_re.finditer(s):
        kind = m.lastgroup
        if kind == "WS":
            continue
        out.append((kind, m.group()))
    return out

def to_rpn(tokens):
    prec = {"+":1, "-":1, "*":2, "/":2, "%":2}
    output, stack = [], []
    for kind, val in tokens:
        if kind == "NUMBER":
            output.append(("NUMBER", float(val)))
        elif kind == "FLOOR":
            stack.append(("FUNC", "floor"))
        elif kind == "OP":
            while stack and stack[-1][0] == "OP" and prec[stack[-1][1]] >= prec[val]:
                output.append(stack.pop())
            stack.append(("OP", val))
        elif kind == "LP":
            stack.append(("LP", val))
        elif kind == "RP":
            while stack and stack[-1][0] != "LP":
                output.append(stack.pop())
            stack.pop()  # LP
            if stack and stack[-1][0] == "FUNC":
                output.append(stack.pop())
        elif kind == "COMMA":
            while stack and stack[-1][0] != "LP":
                output.append(stack.pop())
    while stack:
        output.append(stack.pop())
    return output

def eval_rpn(rpn):
    st = []
    for kind, val in rpn:
        if kind == "NUMBER":
            st.append(val)
        elif kind == "OP":
            b, a = st.pop(), st.pop()
            if val == "+": st.append(a + b)
            elif val == "-": st.append(a - b)
            elif val == "*": st.append(a * b)
            elif val == "/": st.append(a / b)
            elif val == "%": st.append(js_remainder(a, b))
        elif kind == "FUNC":
            x = st.pop()
            st.append(math.floor(x))
    return st[0]

def eval_js_expr(expr: str) -> int:
    expr = expr.strip()
    expr = re.sub(r"\bMath\.floor\s*\(", "floor(", expr)
    if not re.fullmatch(r"[0-9+\-*/()%\s.,floor]+", expr):
        raise ValueError("Unsupported chars in expr")
    tokens = tokenize_expr(expr)
    rpn = to_rpn(tokens)
    val = eval_rpn(rpn)
    return int(val)

def extract_title(prompt: str) -> str:
    # Example prompt contains: for 'Interstellar_medium'
    m = re.search(r"entry summary for\s+['\"]([^'\"]+)['\"]", prompt, re.IGNORECASE)
    if m:
        return m.group(1).strip()

    # Fallback if prompt ever includes a direct /page/summary/<title>
    m = re.search(r"/page/summary/([A-Za-z0-9_()%\-]+)", prompt)
    if m:
        return m.group(1).strip()

    return ""

def fetch_wikipedia_summary(title: str) -> str:
    title = title.strip()
    if not title:
        raise ValueError("Empty Wikipedia title")

    url = "https://en.wikipedia.org/api/rest_v1/page/summary/" + quote(title, safe="")
    r = requests.get(url, timeout=15, headers={"User-Agent":"neon-agent/1.0"})
    if r.status_code == 404 and "_" in title:
        url2 = "https://en.wikipedia.org/api/rest_v1/page/summary/" + quote(title.replace("_", " "), safe="")
        r = requests.get(url2, timeout=15, headers={"User-Agent":"neon-agent/1.0"})
    r.raise_for_status()
    return r.json().get("extract", "")

def nth_word(text: str, n: int) -> str:
    words = re.findall(r"[A-Za-z0-9']+", text)
    if n < 1 or n > len(words):
        return ""
    return words[n-1]

def answer_from_resume(prompt: str) -> str:
    # truthful baseline: compact top part of resume (safe for many questions)
    lines = [l.strip() for l in RESUME_TEXT.splitlines() if l.strip()]
    blob = " ".join(lines[:8])
    return blob[:240] if blob else "N/A"

def nth_space_token(text: str, n: int) -> str:
    tokens = text.strip().split()   # NEON-style: split on whitespace
    if n < 1 or n > len(tokens):
        return ""
    return tokens[n-1]

def handle_prompt(p: str):
    low = p.lower()

    # FORCE keypad mode for handshake/frequency prompts
    if any(k in low for k in ["press", "enter", "keypad", "comm panel"]) or ("respond on frequency" in low):

        # Special-case: "If ... respond on frequency X. All other ... frequency Y."
        m = re.search(
            r"if .*?respond on frequency\s+(\d+).*?all other.*?respond on frequency\s+(\d+)",
            low,
            re.IGNORECASE | re.DOTALL
        )
        if m:
            freq_ai = m.group(1)      # the "if" frequency (AI copilot case)
            freq_other = m.group(2)   # the "all other" frequency
            digits = freq_ai          # WE ARE the AI copilot, so choose this one
        else:
            # fallback: first number mentioned
            nums = re.findall(r"\d+", p)
            digits = nums[0] if nums else NEON_CODE

        if wants_pound(p) and not digits.endswith("#"):
            digits += "#"

        return {"type": "enter_digits", "digits": digits}

    # a) vessel code
    if "vessel authorization code" in low or "neon code" in low:
        digits = NEON_CODE
        if wants_pound(p) and not digits.endswith("#"):
            digits += "#"
        return {"type":"enter_digits","digits":digits}

    # b) computational assessments (Math.floor / arithmetic)
    if "math.floor" in low or "calculate" in low or re.search(r"[\d]\s*[+\-*/%]\s*[\d(]", p):
        expr = None

        # Most prompts put the full expression after the last colon
        if ":" in p:
            expr = p.split(":")[-1].strip()
        else:
            # fallback: grab from first Math.floor occurrence to end
            idx = low.find("math.floor")
            expr = p[idx:].strip() if idx != -1 else None

        if expr:
            try:
                val = eval_js_expr(expr)
            except Exception as e:
                print("Math parse error:", e, "EXPR:", expr)
                val = 0
            digits = str(val) + ("#" if wants_pound(p) else "")
            return {"type":"enter_digits","digits":digits}

    # c) knowledge archive query (Wikipedia)
    if ("knowledge archive" in low) and re.search(r"\b(\d+)(st|nd|rd|th)\b\s+word", low):
        n = int(re.search(r"\b(\d+)(st|nd|rd|th)\b\s+word", low).group(1))

        title = extract_title(p)
        if not title:
            print("Could not parse title from prompt:", p)
            return {"type":"speak_text","text":"error"}

        summary = fetch_wikipedia_summary(title)
        word = nth_word(summary, n) or "N/A"
        exact_n, between_xy = extract_length_constraints(p)
        word = enforce_text_length(word, exact_n, between_xy)
        STATE["spoken_history"].append(word)
        return {"type":"speak_text","text":word}

    # e) verification (recall a word from earlier response)
    if ("verification" in low or "recall" in low or "earlier" in low) and re.search(r"\b(\d+)(st|nd|rd|th)\b\s+word", low):
        n = int(re.search(r"\b(\d+)(st|nd|rd|th)\b\s+word", low).group(1))

        # If NEON specifies which transmission (education/skills/etc), use that
        target = None
        if "education" in low:
            target = "education"
        elif "skills" in low:
            target = "skills"
        elif "work experience" in low or "experience" in low:
            target = "experience"
        elif "project" in low:
            target = "project"
        elif "reason" in low:
            target = "reason"

        # Walk history backwards and pick the first that matches the target keyword, else any
        for prior in reversed(STATE["spoken_history"]):
            if target:
                # crude filter: only consider responses containing a hint word
                if target == "education" and not any(k in prior.lower() for k in ["university", "master", "bachelor", "ms", "bs"]):
                    continue
            out = nth_space_token(prior, n)
            if out:
                exact_n, between_xy = extract_length_constraints(p)
                out = enforce_text_length(out, exact_n, between_xy)
                STATE["spoken_history"].append(out)
                return {"type":"speak_text","text":out}

        out = "N/A"
        STATE["spoken_history"].append(out)
        return {"type":"speak_text","text":out}

    # d) crew manifest (resume)
    if any(k in low for k in ["crew", "manifest", "resume", "background", "education", "skills", "projects", "experience"]):
        ans = answer_from_resume(p)
        exact_n, between_xy = extract_length_constraints(p)
        ans = enforce_text_length(ans, exact_n, between_xy)
        STATE["spoken_history"].append(ans)
        return {"type":"speak_text","text":ans}

    # default
    ans = "ACK"
    STATE["spoken_history"].append(ans)
    return {"type":"speak_text","text":ans}

async def main():
    async with websockets.connect(WS_URL) as ws:
        print("Connected to NEON.")
        while True:
            try:
                raw = await ws.recv()
            except Exception as e:
                print("Disconnected:", e)
                return

            obj = json.loads(raw)

            if obj.get("type") == "challenge":
                prompt = reconstruct_message(obj.get("message", []))
                print("\nPROMPT:", prompt)
                out = handle_prompt(prompt)
                print("REPLY:", out)
                await ws.send(json.dumps(out, ensure_ascii=False))
            else:
                print("INBOUND:", obj)

if __name__ == "__main__":
    asyncio.run(main())