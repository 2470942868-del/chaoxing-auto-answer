#!/usr/bin/env python3
"""
超星学习通自动答题工具 (v5 - Gemini方式)

核心理念：
  1. 不解析DOM题目结构 —— 让AI自己识别
  2. 直接抓取页面全部可见文本 → 喂给DeepSeek
  3. AI自己读出所有题目并给出答案
  4. 我只负责点击（AI点不了页面，需要我操作DOM）

点击部分：找到div.answerBg → 匹配字母 → 调对应onclick函数
"""

import os, sys, json, time, re, random, logging, platform
from typing import Dict, Tuple
from datetime import datetime
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL = "deepseek-chat"
# API Key 配置文件路径（与脚本同目录）
API_KEY_FILE = os.path.join(os.path.dirname(__file__), ".chaoxing_api_key")

if not DEEPSEEK_API_KEY:
    # 尝试从配置文件读取
    if os.path.exists(API_KEY_FILE):
        with open(API_KEY_FILE, "r") as f:
            DEEPSEEK_API_KEY = f.read().strip()
    if not DEEPSEEK_API_KEY:
        # 首次运行，让用户输入
        DEEPSEEK_API_KEY = input("🔑 请输入 DeepSeek API Key（https://platform.deepseek.com/api_keys）\n> ").strip()
        if not DEEPSEEK_API_KEY:
            print("❌ 未输入 API Key"); sys.exit(1)
        # 保存到配置文件
        try:
            with open(API_KEY_FILE, "w") as f:
                f.write(DEEPSEEK_API_KEY)
            print(f"✅ API Key 已保存到 {API_KEY_FILE}")
        except Exception as e:
            print(f"⚠️ 保存 API Key 失败: {e}")

SAVE_RESULT = True
RESULT_FILE = "chaoxing_result.json"

# ── 调试与速度控制 ──
DEBUG_INSPECT_FIRST_N = 0      # 设 >0 时打印前 N 题详细结构（调bug用）
CLICK_DELAY_MIN = 0.2          # 题间最小延迟（秒）
CLICK_DELAY_MAX = 0.5          # 题间最大延迟（秒）

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("chaoxing")


# ============================================================
# 第一步：抓取页面全部文本 + 采集选项结构
# ============================================================

def collect_page_data(page) -> Tuple[str, int]:
    """
    返回 (full_text, stem_count)

    full_text: document.body.innerText — 喂给AI用的全部文本
    stem_count: div.stem_answer 的数量（用于校验，点击时重查DOM）
    """
    log.info("📖 采集页面数据...")

    try:
        page.wait_for_selector("div.stem_answer", timeout=15000)
    except PwTimeout:
        log.error("❌ 页面加载超时")
        return "", 0

    full_text = page.evaluate("() => document.body.innerText")
    log.info(f"  📄 页面文本长度: {len(full_text)} 字符")

    stems = page.query_selector_all("div.stem_answer")
    count = len(stems)
    log.info(f"  📦 div.stem_answer: {count} 个")

    return full_text, count


# ============================================================
# 第二步：喂给DeepSeek，让AI自己回答
# ============================================================

def ask_deepseek(full_text: str) -> str:
    """
    把页面文本直接喂给DeepSeek，让它自己识别题目并给出答案。
    """
    log.info("🤖 发送给DeepSeek分析...")

    prompt = f"""你是一个考试答题助手。下面是一个学习通作业页面的全部文本内容。

请完成以下任务：
1. 识别出页面中的所有题目（单选题、多选题、判断题、填空题）
2. 根据你的知识给出每道题的正确答案

输出格式要求：
- 每行一个答案，格式严格为"数字: 答案"
- 单选题输出单个字母，如 "1: C"
- 多选题输出多个字母连写，如 "2: ABD"
- 判断题正确的输出A、错误的输出B，如 "3: A"
- 填空题直接输出文本答案，如 "56: 物质决定意识"
- 填空题如有多个空，用逗号分隔，如 "57: 实践是基础, 理论是指导"
- 不要输出任何解释和其他内容

以下是页面文本：
---
{full_text}
---

现在请按格式输出所有题的答案："""

    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": "你是一个考试答题助手。只输出答案，每行格式严格为\"数字: 答案\"。填空题输出完整文本，选择题输出字母，不要输出任何解释。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.05,
        "max_tokens": 4000,
    }
    t0 = time.time()

    for attempt in range(3):
        try:
            resp = requests.post(DEEPSEEK_API_URL, headers=headers, json=payload, timeout=180)
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            elapsed = time.time() - t0
            log.info(f"  ✅ DeepSeek 返回 ({elapsed:.1f}s)")
            break
        except (requests.exceptions.ConnectionError,
                requests.exceptions.SSLError,
                requests.exceptions.Timeout):
            log.warning(f"  📡 网络异常，重试 {attempt+1}/3")
            if attempt == 2:
                return ""
            time.sleep(3)
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response else 0
            if status == 429:
                log.warning(f"  ⏱ 速率限制，重试 {attempt+1}/3")
                time.sleep(5)
                continue
            if attempt == 2:
                log.error(f"  ❌ HTTP {status}: {e}")
                return ""
        except (KeyError, json.JSONDecodeError) as e:
            if attempt == 2:
                log.error(f"  ❌ API响应解析失败: {e}")
                return ""
        except requests.exceptions.RequestException as e:
            if attempt == 2:
                log.error(f"  ❌ 请求异常: {e}")
                return ""
            log.warning(f"  📡 请求异常，重试 {attempt+1}/3")
            time.sleep(3)
    else:
        log.error("  ❌ 全部重试失败")
        return ""

    # 打印AI的原始输出
    log.info(f"  📝 AI原始输出:")
    for line in content.strip().split("\n")[:10]:
        log.info(f"    {line}")
    line_count = len(content.strip().split("\n"))
    if line_count > 10:
        log.info(f"    ... 共{line_count}行")

    return content


def parse_ai_response(content: str) -> Dict[int, str]:
    """解析AI返回的答案，支持字母（选择题）和文本（填空题）。"""
    answers = {}
    for line in content.strip().split("\n"):
        line = line.strip()
        if not line:
            continue

        # 格式1: "第1题: A" / "1题: A" / "1: A" / "1：A" / "1: 物质决定意识"
        m = re.search(r"(?:第)?(\d+)\s*[题]?\s*[：:]\s*(.+)", line)
        if m:
            val = m.group(2).strip()
            # 如果答案只是纯字母（A/B/C/D/AB/ABC/ABCD），保持大写
            if re.match(r"^[A-Da-d]+$", val):
                val = val.upper()
            answers[int(m.group(1))] = val
            continue

        # 格式2: "1. A" / "1、A" / "1. 物质决定意识"
        m = re.match(r"^\s*(\d+)\s*[.．、\s]\s*(.+)", line)
        if m:
            val = m.group(2).strip()
            if re.match(r"^[A-Da-d]+$", val):
                val = val.upper()
            answers[int(m.group(1))] = val
    return answers


# ============================================================
# 第三步：点击答案
# ============================================================

def debug_inspect_stem(page, d_idx: int):
    """实时查询第 d_idx 题的结构，打印详细调试信息。"""
    log.info(f"  🔍 [Debug #{d_idx}] 实时查看第 {d_idx} 题:")
    try:
        # 通过序号定位到页面上具体第 1 题的 questionLi 容器
        # 从全页面找第 d_idx 个 questionLi 或 stem_answer
        qli = page.query_selector_all("div.questionLi")
        target = None
        if d_idx - 1 < len(qli):
            target = qli[d_idx - 1]
        if not target:
            log.warning(f"    找不到 div.questionLi[{d_idx-1}]")
            return

        stem = target.query_selector("div.stem_answer")
        if not stem:
            log.warning(f"    questionLi 内无 div.stem_answer")
            return

        opts = stem.query_selector_all("div.answerBg")
        log.info(f"    div.answerBg 个数: {len(opts)}")

        for j, opt in enumerate(opts):
            onclick = (opt.get_attribute("onclick") or "N/A")[:50]
            aria = (opt.get_attribute("aria-label") or "N/A")[:80]
            # 尝试各种方式提取字母
            labels_found = []
            for sel in ["span.num_option_dx", "span.num_option", "span[class*='option']", "span"]:
                sp = opt.query_selector(sel)
                if sp:
                    data_attr = sp.get_attribute("data") or "∅"
                    txt = (sp.text_content() or "∅").strip()[:10]
                    html = (sp.inner_html() or "∅")[:20]
                    labels_found.append(f"sel={sel} data={data_attr} text={txt}")
            log.info(f"    opt[{j}]: onclick={onclick}")
            log.info(f"            aria-label={aria}")
            for lf in labels_found:
                log.info(f"            {lf}")
    except Exception as e:
        log.warning(f"    调试检查异常: {e}")


def check_question_type(page, question_idx: int) -> str:
    """
    检查题型：
    - 'choice' : 单选/多选/判断（有 answerBg 可点击）
    - 'blank'  : 填空题（有 input/textarea 需填写）
    - 'subjective' : 主观题（其他）
    """
    js = f"""
    (() => {{
        const containers = document.querySelectorAll('div.questionLi');
        const container = containers[{question_idx} - 1];
        if (!container) return 'unknown';

        // 检查 answertype hidden input: 0=单选, 1=多选, 2=填空, 3=判断
        const typeInput = container.querySelector('input[id^="answertype"]');
        if (typeInput) {{
            const val = typeInput.value;
            if (val === '0' || val === '1' || val === '3') return 'choice';
            if (val === '2') return 'blank';
            return 'subjective';
        }}

        // fallback: 检测输入框
        const stem = container.querySelector('div.stem_answer');
        if (stem) {{
            if (stem.querySelector('div.answerBg')) return 'choice';
            if (stem.querySelector('textarea, input[type="text"]')) return 'blank';
        }}
        return 'subjective';
    }})()
    """
    return page.evaluate(js)


def click_option_by_js(page, question_idx: int, letter: str) -> bool:
    """
    完全在浏览器 JS 环境中定位并点击选项，绕过所有 ElementHandle 问题。

    执行流程：
    1. 通过序号找到第 N 个 div.questionLi
    2. 在其中找到 div.stem_answer
    3. 遍历 div.answerBg，匹配 aria-label 首字母
    4. 找到后调用对应的 addChoice / addMultipleChoice
    5. 滚动到可见位置
    """
    js = f"""
    (() => {{
        try {{
            const containers = document.querySelectorAll('div.questionLi');
            if (containers.length < {question_idx}) {{
                const stems = document.querySelectorAll('div.stem_answer');
                if (stems.length < {question_idx}) return {{ok: false, reason: 'no container'}};
                var stem = stems[{question_idx} - 1];
            }} else {{
                var container = containers[{question_idx} - 1];
                var stem = container.querySelector('div.stem_answer');
                if (!stem) {{ stem = container; }}
            }}

            const opts = stem.querySelectorAll('div.answerBg');
            if (!opts.length) return {{ok: false, reason: 'no answerBg', count: 0}};

            const targetLetter = '{letter}';
            for (let i = 0; i < opts.length; i++) {{
                const opt = opts[i];
                const aria = (opt.getAttribute('aria-label') || '').trim().toUpperCase();
                const ariaLetter = aria.charAt(0);
                if (ariaLetter === targetLetter) {{
                    opt.scrollIntoView({{behavior: 'instant', block: 'center'}});
                    // 用原生 click() 触发 onclick，浏览器自动绑定 this
                    opt.click();
                    return {{ok: true, method: 'aria', index: i}};
                }}
            }}

            // fallback: 用 span.data 再试一次
            for (let i = 0; i < opts.length; i++) {{
                const opt = opts[i];
                let span = opt.querySelector('span.num_option_dx, span.num_option, span[class*="option"]');
                if (!span) span = opt.querySelector('span');
                if (span) {{
                    const data = span.getAttribute('data') || span.textContent || '';
                    if (data.trim().toUpperCase() === targetLetter) {{
                        opt.scrollIntoView({{behavior: 'instant', block: 'center'}});
                        opt.click();
                        return {{ok: true, method: 'span', index: i}};
                    }}
                }}
            }}

            return {{ok: false, reason: 'no match', optsCount: opts.length, letters: Array.from(opts).map(o => (o.getAttribute('aria-label')||'')[0])}};
        }} catch (e) {{
            const stk = e.stack || '';
            return {{ok: false, reason: e.message, stack: stk.slice(0,200)}};
        }}
    }})()
    """
    result = page.evaluate(js)
    return result


def fill_blank(page, answer: str, question_idx: int = -1) -> bool:
    """在浏览器 JS 环境中找到填空题的输入框并填入答案。"""
    # 多个空用逗号分隔
    parts = [p.strip() for p in answer.split(",") if p.strip()]
    if not parts:
        parts = [answer]

    js_template = """
    (() => {
        const containers = document.querySelectorAll('div.questionLi');
        const container = containers[{idx} - 1];
        if (!container) return {ok: false, reason: 'no container'};

        // 找到所有可输入的字段（只匹配文本输入框，不匹配 radio/checkbox）
        const inputs = container.querySelectorAll(
            'div.stem_answer textarea, ' +
            'div.stem_answer input[type="text"]'
        );
        if (!inputs.length) return {ok: false, reason: 'no input fields'};

        // 如果输入框数量 > 答案部分数，尝试用 split 查找多个空
        const values = {parts_json};
        for (let i = 0; i < Math.min(inputs.length, values.length); i++) {
            const inp = inputs[i];
            inp.scrollIntoView({behavior: 'instant', block: 'center'});
            // 清空并填入
            if (inp.tagName === 'TEXTAREA' || inp.tagName === 'INPUT') {
                const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                )?.set || Object.getOwnPropertyDescriptor(
                    window.HTMLTextAreaElement.prototype, 'value'
                )?.set;
                if (nativeInputValueSetter) {
                    nativeInputValueSetter.call(inp, values[i]);
                } else {
                    inp.value = values[i];
                }
                // 触发 input/change 事件（超星可能绑定监听）
                inp.dispatchEvent(new Event('input', {bubbles: true}));
                inp.dispatchEvent(new Event('change', {bubbles: true}));
            }
        }
        return {ok: true, filled: Math.min(inputs.length, values.length)};
    })()
    """.format(idx=question_idx, parts_json=json.dumps(parts))

    result = page.evaluate(js_template)
    ok = result.get("ok", False)
    if ok:
        log.info(f"    ✓ 填空: {', '.join(parts)} ({result.get('filled', '?')} 空)")
    else:
        log.warning(f"    ✗ 填空失败: {result.get('reason', '?')}")
    return ok


def click_answer(page, answer: str, question_idx: int = -1) -> bool:
    """在浏览器 JS 环境中点击指定题目的选项。answer: 'C' 或 'ABD'"""
    raw = answer.strip().upper()
    letters = list(raw)
    if not letters:
        return False

    clicked_letters = []
    failed_letters = []

    for letter in letters:
        result = click_option_by_js(page, question_idx, letter)
        ok = result.get("ok", False)
        if ok:
            clicked_letters.append(letter)
            log.info(f"    ✓ {letter}")
        else:
            failed_letters.append(letter)
            log.warning(f"    ✗ {letter} (原因: {result.get('reason','?')} opts: {result.get('optsCount','?')})")

    if clicked_letters:
        log.info(f"    ✓ 已选: {''.join(clicked_letters)}")
    if failed_letters:
        log.warning(f"    ✗ 失败: {''.join(failed_letters)}")

    return len(clicked_letters) > 0


def run():
    if not DEEPSEEK_API_KEY:
        log.error("❌ 未设置 DEEPSEEK_API_KEY"); sys.exit(1)

    print("\n" + "="*60)
    print("📚 超星学习通自动答题助手 v5")
    print("="*60 + "\n")

    # ── 获取作业URL ──
    work_url = os.environ.get("CHAOXING_WORK_URL", "")
    if not work_url:
        work_url = input("🔗 粘贴作业URL后按 Enter\n> ").strip()
        if not work_url:
            log.error("❌ 未输入URL"); sys.exit(1)
    log.info(f"🌐 作业URL: {work_url[:80]}...")

    with sync_playwright() as p:
        # Windows 上用系统安装的 Chrome，避免下载捆绑版 Chromium
        launch_opts = {"headless": False, "args": ["--disable-blink-features=AutomationControlled"]}
        if platform.system() == "Windows":
            launch_opts["channel"] = "chrome"
        browser = p.chromium.launch(**launch_opts)
        ctx = browser.new_context(viewport={"width":1280,"height":900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36")
        page = ctx.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get:()=>undefined})")

        log.info("🌐 打开作业页面...")
        page.goto(work_url, wait_until="domcontentloaded", timeout=30000)
        try:
            if page.query_selector("input[name='unameid'], div[class*='login']"):
                log.info("🔐 请登录...")
                page.wait_for_url("**/dowork*", timeout=0)
                log.info("✅ 登录成功")
        except Exception as e:
            log.debug(f"  登录检测异常: {e}")
        input("🔔 确认已看到作业内容后按 Enter\n")

        # ====== 第一步：采集 ======
        full_text, stem_count = collect_page_data(page)
        if not stem_count:
            log.error("❌ 未找到题目")
            page.screenshot(path=os.path.join(os.path.dirname(__file__), "debug_screenshot.png"))
            input("\n按 Enter"); browser.close(); return

        # ====== 第二步：AI分析 ======
        ai_output = ask_deepseek(full_text)
        answers_map = parse_ai_response(ai_output)

        # 打印答案
        print(); log.info("📊 AI给出的答案:")
        for idx in sorted(answers_map.keys()):
            log.info(f"  [{idx}] {answers_map[idx]}")

        # ====== 第三步：交互（每次重新查DOM，完全隔离题型）=====
        print(); log.info("🖱️ 开始作答...")
        choice_ok = choice_fail = blank_ok = blank_fail = subj_skip = unk_skip = no_answer = 0

        for i in range(stem_count):
            idx = i + 1
            need_sleep = False

            # ── 整题隔离：任何异常不影响下一题 ──
            try:
                answer = answers_map.get(idx)
                if not answer:
                    no_answer += 1
                    log.warning(f"  [{idx}] 无答案，跳过")
                    continue

                # ── 题型分发（严格隔离，互不影响）──
                qtype = check_question_type(page, idx)

                if qtype == 'blank':
                    if fill_blank(page, answer, question_idx=idx):
                        blank_ok += 1
                        need_sleep = True
                    else:
                        blank_fail += 1
                        log.warning(f"  [{idx}] ❌ 填空失败")

                elif qtype == 'choice':
                    if idx <= DEBUG_INSPECT_FIRST_N:
                        debug_inspect_stem(page, idx)
                    if click_answer(page, answer, question_idx=idx):
                        choice_ok += 1
                        need_sleep = True
                    else:
                        choice_fail += 1
                        log.warning(f"  [{idx}] ❌ 点击失败")

                elif qtype == 'subjective':
                    subj_skip += 1
                    log.info(f"  [{idx}] ⏭️ 主观题，跳过")

                else:
                    unk_skip += 1
                    log.warning(f"  [{idx}] ⏭️ 未知题型，跳过")

            except Exception as e:
                log.warning(f"  [{idx}] 💥 意外异常: {type(e).__name__}: {e}，跳过本题")
                unk_skip += 1

            # 成功后才延迟，跳过/失败不等待
            if need_sleep:
                time.sleep(random.uniform(CLICK_DELAY_MIN, CLICK_DELAY_MAX))

            # 进度提示（每 5 题）
            if idx % 5 == 0:
                parts = []
                if choice_ok: parts.append(f"✅{choice_ok}")
                if blank_ok: parts.append(f"📝{blank_ok}")
                if choice_fail or blank_fail: parts.append(f"❌{choice_fail+blank_fail}")
                if subj_skip: parts.append(f"⏭️{subj_skip}")
                if no_answer: parts.append(f"❓{no_answer}")
                log.info(f"  📊 进度: {idx}/{stem_count} | {' | '.join(parts)}")

        # ====== 汇总 ======
        print(); print("="*60); print("📊 完成"); print("="*60)
        log.info(f"总题数: {stem_count}")
        log.info(f"  选择题: ✅ {choice_ok} | ❌ {choice_fail}")
        log.info(f"  填空题: 📝 {blank_ok} | ❌ {blank_fail}")
        log.info(f"  主观题跳过: ⏭️ {subj_skip}")
        log.info(f"  AI未出答案: ❓ {no_answer}")
        if unk_skip:
            log.info(f"  未知题型跳过: ⚠️ {unk_skip}")

        if SAVE_RESULT:
            result_path = os.path.join(os.path.dirname(__file__), RESULT_FILE)
            with open(result_path, "w", encoding="utf-8") as f:
                json.dump({
                    "timestamp": datetime.now().isoformat(),
                    "total": stem_count,
                    "choice": {"ok": choice_ok, "fail": choice_fail},
                    "blank": {"ok": blank_ok, "fail": blank_fail},
                    "no_answer": no_answer,
                    "subjective_skip": subj_skip,
                    "unknown_skip": unk_skip,
                    "answers": {str(k): v for k, v in sorted(answers_map.items())},
                }, f, ensure_ascii=False, indent=2)

        print("\n✅ 完成！检查后手动提交。")
        input("\n按 Enter 关闭...")
        browser.close()

if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("\n👋 退出")
    except Exception as e:
        log.exception(f"💥 {e}")
        sys.exit(1)
