#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# v4 (2026-11-22, 基于 eooce/Auto-Renew-Bothosting 上游原版 + 之前 v1-v3 的修复):
# - 中文日期/按钮/冷却倒计时识别 (上游原版没有, 中文账单页会读不到日期)
# - 弹窗按钮按"续期 4 天"优先选, 不盲点第一个 .btn-primary
# - 真实点击优先, JS 点击兜底; 点完弹窗还在就再点一次
# - 点完轮询 90 秒, 按"今天+4天"目标日期判断, 识别失败提示
# - 刷新后没变再等 60 秒复查一次, 两次都没变才报可能未成功

import os, re, sys, time, random, requests, subprocess,ctypes
from datetime import datetime, timedelta, timezone
from seleniumbase import SB

# 环境变量配置(可以直接私库在双引号里填写)
EMAIL         = os.environ.get("EMAIL") or ""           # 邮箱必填，登录和通知使用
PASSWORD      = os.environ.get("PASSWORD") or ""        # 主用密码登录方式, 失败时才使用,必须填写
TG_CHAT_ID    = os.environ.get("TG_CHAT_ID") or ""      # TG chat id,不填写不通知，需和bot token一起填写生效
TG_BOT_TOKEN  = os.environ.get("TG_BOT_TOKEN") or ""    # TG bot token 

BASE_URL = "https://bot-hosting.net"

if not (EMAIL and PASSWORD):
    print("ℹ️ 未配置 EMAIL 和 PASSWORD,脚本终止。")
    sys.exit(1)

# 发送tg通知
def send_telegram_message(message: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("⚠️ Telegram 未配置，跳过通知")
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": message}, timeout=10)
        print("✅ Telegram 通知已发送")
    except Exception as e:
        print(f"❌ Telegram 发送失败: {e}")

# 通知格式
def format_notification(status: str, extra: str = "", error: str = "", expiry_date: str = "") -> str:
    local_time = time.gmtime(time.time() + 8 * 3600)
    now = time.strftime("%Y-%m-%d %H:%M:%S", local_time)
    if '@' in EMAIL:
        name, domain = EMAIL.split('@', 1)
        if len(name) > 4:
            masked_email = f"{name[:2]}****{name[-2:]}@{domain}"
        else:
            masked_email = f"{name}@{domain}"
    else:
        masked_email = EMAIL[:2] + '****' 
    
    lines = [
        "🇫🇮 Bot-hosting 续期通知",
        "",
        f"{status}",
        f"👤 登录账户: {masked_email}",
    ]
    if expiry_date:
        lines.append(f"📅 到期时间: {expiry_date}")
    if extra:
        lines.append(extra)
    if error:
        lines.append(f"⚠️ 错误信息: {error}")
    lines.append(f"⏱️ 登录时间: {now}")
    return "\n".join(lines)

# ---------- 基础工具 ----------
def js(sb, script, default=None, errors=None):
    try:
        result = sb.execute_script(script)
        return default if result is None else result
    except Exception as e:
        if errors is not None:
            errors.append(f"execute_script: {type(e).__name__}: {e}")
        try:
            result = sb.cdp.evaluate("(function(){ " + script + " })()")
            return default if result is None else result
        except Exception as e2:
            if errors is not None:
                errors.append(f"cdp.evaluate: {type(e2).__name__}: {e2}")
            return default

# Turnstile 定位失败时的现场诊断快照
def diag_turnstile_dom(sb, errors=None):
    snap = js(sb, """
        var out = [];
        var ifr = document.querySelectorAll('iframe');
        out.push('top-doc iframes: ' + ifr.length);
        for (var i = 0; i < ifr.length; i++) {
            var r = ifr[i].getBoundingClientRect();
            out.push('iframe w=' + Math.round(r.width) + ' h=' + Math.round(r.height) + ' src=' + (ifr[i].src || '').slice(0, 90));
        }
        var ts = document.querySelectorAll('[class*="turnstile"], [id*="turnstile"], input[name="cf-turnstile-response"]');
        out.push('turnstile-ish elements: ' + ts.length);
        for (var j = 0; j < ts.length; j++) {
            var rr = ts[j].getBoundingClientRect();
            var cls = (typeof ts[j].className === 'string') ? ts[j].className : (ts[j].getAttribute('class') || '');
            out.push(' - ' + ts[j].tagName + ' class=' + cls + ' w=' + Math.round(rr.width) + ' h=' + Math.round(rr.height));
        }
        out.push('window.turnstile=' + (typeof window.turnstile) + ' readyState=' + document.readyState);
        return out.join('\\n');
    """, "")
    for line in str(snap).splitlines():
        print("   [diag] " + line)
    if errors:
        for err in errors:
            print("   [diag][exec-error] " + str(err)[:200])

#  页面注入脚本
# Turnstile token（官方 JS 接口 或 表单隐藏域），未通过时为空串
TURNSTILE_TOKEN_JS = """
try { if (window.turnstile && window.turnstile.getResponse) {
        var t = window.turnstile.getResponse(); if (t) return t; } } catch (e) {}
var el = document.querySelector('input[name="cf-turnstile-response"]');
return el ? (el.value || '') : '';
"""

TURNSTILE_EXISTS_JS = """
return document.querySelector(
    'input[name="cf-turnstile-response"], iframe[src*="challenges.cloudflare.com"], .cf-turnstile'
) !== null;
"""
TS_COORDS_JS = """
function findWidget(root) {
    var els = root.querySelectorAll(
        'iframe[src*="challenges.cloudflare.com"], .cf-turnstile, [id*="cf-chl"]');
    for (var i = 0; i < els.length; i++) {
        var r = els[i].getBoundingClientRect();
        if (r.width > 50 && r.height > 20) return els[i];
    }
    var all = root.querySelectorAll('*');
    for (var j = 0; j < all.length; j++) {
        if (all[j].shadowRoot) {
            var f = findWidget(all[j].shadowRoot);
            if (f) return f;
        }
    }
    return null;
}
function boxFromTokenInput() {
    var el = document.querySelector('input[name="cf-turnstile-response"]');
    var cur = el ? el.parentElement : null;
    for (var k = 0; k < 6 && cur; k++) {
        var r = cur.getBoundingClientRect();
        if (r.width > 100 && r.width < 600 && r.height > 30 && r.height < 140) return cur;
        cur = cur.parentElement;
    }
    return null;
}
var hit = findWidget(document) || boxFromTokenInput();
if (!hit) return null;
var hr = hit.getBoundingClientRect();
return {cx: Math.round(hr.x + 30), cy: Math.round(hr.y + hr.height / 2)};
"""

# Turnstile 脚本异步渲染，轮询等待组件出现
def wait_turnstile_present(sb, timeout=12):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if js(sb, TURNSTILE_EXISTS_JS, False):
            return True
        time.sleep(1)
    return False

def _activate_window():
    if os.name == "nt":
        return

    for cls in ["chrome", "chromium", "Chromium", "Chrome", "google-chrome"]:
        try:
            r = subprocess.run(["xdotool", "search", "--onlyvisible", "--class", cls], capture_output=True, text=True, timeout=3)
            wids = [w for w in r.stdout.strip().split("\n") if w.strip()]
            if wids:
                subprocess.run(["xdotool", "windowactivate", "--sync", wids[0]], timeout=3, stderr=subprocess.DEVNULL)
                time.sleep(0.2)
                return
        except Exception:
            pass
    try:
        subprocess.run(["xdotool", "getactivewindow", "windowactivate"], timeout=3, stderr=subprocess.DEVNULL)
    except Exception:
        pass

def _windows_mouse_click(x: int, y: int):
    try:
        user32 = ctypes.windll.user32
        user32.SetCursorPos(int(x), int(y))
        time.sleep(0.08)
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        time.sleep(0.03)
        user32.mouse_event(0x0004, 0, 0, 0, 0)
    except Exception:
        pass

def _xdotool_click(x: int, y: int):
    if os.name == "nt":
        _windows_mouse_click(x, y)
        return

    _activate_window()
    try:
        subprocess.run(["xdotool", "mousemove", "--sync", str(x), str(y)], timeout=3, stderr=subprocess.DEVNULL)
        time.sleep(0.15)
        subprocess.run(["xdotool", "click", "1"], timeout=2, stderr=subprocess.DEVNULL)
    except Exception:
        os.system(f"xdotool mousemove {x} {y} click 1 2>/dev/null")

def turnstile_token(sb) -> str:
    token = js(sb, TURNSTILE_TOKEN_JS, "")
    return token if isinstance(token, str) else ""

def _click_turnstile(sb, locate_timeout=10) -> bool:
    coords = None
    errors = []
    deadline = time.time() + locate_timeout
    while time.time() < deadline:
        coords = js(sb, TS_COORDS_JS, None, errors)
        if coords:
            break
        time.sleep(1)
    if not coords:
        print("⚠️ 无法定位 Turnstile 坐标, 打印现场诊断:")
        diag_turnstile_dom(sb, errors)
        return False

    try:
        wi = js(sb, "return '' + (window.screenX || 0) + '|' + (window.screenY || 0) + '|' +"
                    " window.outerHeight + '|' + window.innerHeight;", "")
        parts = str(wi).split("|")
        sx, sy = int(parts[0] or 0), int(parts[1] or 0)
        bar = int(parts[2] or 800) - int(parts[3] or 768)
        ax = int(coords["cx"]) + sx
        ay = int(coords["cy"]) + sy + bar
        print(f"🖱️ 底层点击 Turnstile ({ax}, {ay})")
        _xdotool_click(ax, ay)
        time.sleep(3)
        return True
    except Exception as e:
        print(f"⚠️ 底层点击失败: {e}")
        return False

def click_turnstile(sb) -> bool:
    if _click_turnstile(sb):
        return True
    coords = js(sb, TS_COORDS_JS)
    if coords:
        try:
            if hasattr(sb, "cdp") and hasattr(sb.cdp, "gui_click_x_y"):
                print(f"🖱️ 坐标点击 Turnstile ({coords['cx']},{coords['cy']})")
                sb.cdp.gui_click_x_y(int(coords["cx"]), int(coords["cy"]))
                return True
        except Exception as e:
            print(f"⚠️ 坐标点击失败: {e}")
    try:
        if hasattr(sb, "cdp") and hasattr(sb.cdp, "click_captcha"):
            print("🖱️ 兜底调用cdp点击 ...")
            sb.cdp.click_captcha()
            return True
        print("⚠️ 当前 SeleniumBase 版本无 sb.cdp.click_captcha")
    except Exception as e:
        print(f"⚠️ click_captcha 出错: {e}")
    return False

def solve_turnstile(sb, timeout=90, tag="Turnstile", first_wait=8) -> bool:
    if turnstile_token(sb):
        return True

    deadline = time.time() + timeout
    next_click = time.time() + first_wait
    clicks = 0
    while time.time() < deadline:
        if turnstile_token(sb):
            print(f"✅ {tag}已通过(已获取 token)")
            return True
        if time.time() >= next_click:
            clicks += 1
            print(f"🖱️ 第 {clicks} 次点击 Turnstile 验证框...")
            click_turnstile(sb)
            next_click = time.time() + 10 + random.uniform(0, 3)
        time.sleep(2)
    ok = bool(turnstile_token(sb))
    print(f"{'✅' if ok else '❌'} {tag}{'已通过' if ok else '未通过'}")
    return ok

def handle_turnstile(sb) -> bool:
    print("🛡️ 处理 Turnstile 验证...")
    return solve_turnstile(sb, timeout=90, tag="Turnstile", first_wait=8)
    
# 获取当前出口ip
def get_current_ip(proxy_server: str = "") -> str:
    proxies = None
    if proxy_server:
        proxies = {"http": proxy_server, "https": proxy_server}
    response = requests.get("https://api.ip.sb/ip", proxies=proxies, timeout=15)
    response.raise_for_status()
    return response.text.strip()

# 时间格式化
def format_countdown(countdown_str: str) -> str:
    try:
        h, m, _ = countdown_str.split(':')
        h = int(h)
        m = int(m)
        if h > 0:
            return f"{h}h{m}min"
        else:
            return f"{m}min"
    except:
        return countdown_str

# 归一化日期为 YYYY/MM/DD
def normalize_date(date_str: str) -> str:
    # 中文: 2026年11月18日 -> 2026/11/18
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", date_str)
    if m:
        return f"{m.group(1)}/{int(m.group(2)):02d}/{int(m.group(3)):02d}"
    # 英文月份名: 18 Nov 2026 / Nov 18, 2026 -> 2026/11/18
    months = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
              "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})", date_str)
    if m and m.group(2).lower()[:3] in months:
        return f"{m.group(3)}/{months[m.group(2).lower()[:3]]:02d}/{int(m.group(1)):02d}"
    m = re.search(r"([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(\d{4})", date_str)
    if m and m.group(1).lower()[:3] in months:
        return f"{m.group(3)}/{months[m.group(1).lower()[:3]]:02d}/{int(m.group(2)):02d}"
    parts = date_str.split('/')
    if len(parts) == 3:
        if len(parts[2]) == 4:   # MM/DD/YYYY
            return f"{parts[2]}/{int(parts[0]):02d}/{int(parts[1]):02d}"
        if len(parts[0]) == 4:   # YYYY/MM/DD
            return f"{parts[0]}/{int(parts[1]):02d}/{int(parts[2]):02d}"
    return date_str

DATE_RE = r"(\d{4}/\d{1,2}/\d{1,2}|\d{1,2}/\d{1,2}/\d{4})"

# 获取过期日期
DATE_RE = r"(\d{4}年\d{1,2}月\d{1,2}日|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}|\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2})"
def extract_expiry_date(page_source: str) -> str:
    patterns = [
        r"[Ee]xpires\s*[:\-]?\s*" + DATE_RE,   # 优先匹配包含 "Expires" 的
        r"(\d{4}年\d{1,2}月\d{1,2}日)\s*到期",  # 中文: 2026年11月18日 到期
        DATE_RE,                                # 兜底匹配
    ]
    
    for pattern in patterns:
        match = re.search(pattern, page_source)
        if match:
            # match.group(1) 现在能准确拿到完整的日期字符串
            return normalize_date(match.group(1))
            
    return None

# 优先从 DOM 的 st-meta 元素提取到期日期 (不依赖页面语言)
def get_expiry_from_dom(sb) -> str:
    for selector in ['span.st-meta', '.st-meta', '[class*="st-meta"]']:
        try:
            if sb.is_element_present(selector):
                match = re.search(DATE_RE, sb.get_text(selector))
                if match:
                    return normalize_date(match.group(1))
        except Exception:
            pass
    return None

# 主流程
def main():
    print("#" * 25)
    print("   Bot-hosting 自动续期")
    print("#" * 25)

    IS_PROXY = os.environ.get("IS_PROXY", "false").lower() == "true"
    PROXY_SERVER = os.environ.get("PROXY_SERVER", "").strip() or "http://127.0.0.1:1080"
    HEADLESS = os.environ.get("HEADLESS", "false").lower() == "true" 

    sb_kwargs = {"uc": True, "headless": HEADLESS, "locale_code": "en-US"}

    if IS_PROXY:
        print(f"🔗 挂载代理: {PROXY_SERVER}")
        sb_kwargs["proxy"] = PROXY_SERVER
    else:
        print("🍭 未使用代理，直连访问")

    with SB(**sb_kwargs) as sb:
        try:
            ip = get_current_ip(PROXY_SERVER if IS_PROXY else "")
            print(f"📍 当前出口IP: {ip}")
        except Exception as e:
            print(f"⚠️ 获取出口 IP 失败: {e}")

        login_ok = False

        # 账号密码登录（密码不过期，唯一登录方式；整段兜异常，失败只标记 login_ok=False 走统一失败通知）
        if EMAIL and PASSWORD:
            print("🚀 启动浏览器...")
            try:
                print(f"🌐 访问 {BASE_URL}/login")
                sb.open(f"{BASE_URL}/login")
                sb.wait_for_ready_state_complete()
                sb.sleep(3)
                sb.click('button[type="submit"]') # 点击 Continue with email 按钮
                sb.sleep(12)  # 等待页面加载
                print(f"📧 填写邮箱...")
                sb.input('input[type="email"]', EMAIL)
                sb.sleep(1)
                print(f"🔑 填写密码...")
                sb.input('input[type="password"]', PASSWORD)
                sb.sleep(3)

                if wait_turnstile_present(sb):
                    if not handle_turnstile(sb):
                        print("❌ 登录界面的 Turnstile 验证失败，尝试兜底点击...")
                        try:
                            sb.cdp.click_captcha()  # 再次尝试点击 Turnstile 验证
                            sb.sleep(4)
                        except Exception as e:
                            print(f"⚠️ 兜底点击失败: {e}")
                        # sb.save_screenshot("turnstile_failed.png")
                else:
                    print("ℹ️ 未检测到 Turnstile")
                sb.sleep(1)
                sb.click('button[type="submit"]') # 点击登录按钮
                print("🖱️ 提交登录按钮")
                sb.sleep(6)  # 等待页面加载
                sb.wait_for_ready_state_complete()
                sb.open(f"{BASE_URL}/dash/billings")
                sb.wait_for_ready_state_complete()
                sb.sleep(3)
                current_url = sb.get_current_url()
                current_title = sb.get_title()
                print(f"📝 当前URL: {current_url}, Title: {current_title}")

                if "/dash/billings" in current_url and "/login" not in current_url and "error=" not in current_url:
                    login_ok = True
                    print("✅ Password 登录成功,当前已到达账单页")
                else:
                    print(f"❌ Password 登录后仍未到达账单页，当前URL: {current_url}")
            except Exception as e:
                    print(f"❌ Password 登录过程异常: {type(e).__name__}: {str(e)[:150]}")

        if not login_ok:
            send_telegram_message(format_notification("❌ 登录失败", error="Password 登录失败"))
            return

        # 提取当前到期日期
        sb.sleep(3)
        try:
            page_source = sb.get_page_text() + "\n" + sb.get_page_source()
        except Exception:
            page_source = sb.get_page_source()
        current_expiry = get_expiry_from_dom(sb) or extract_expiry_date(page_source)
        if current_expiry:
            print(f"📅 当前到期日期: {current_expiry}")
        else:
            print("⚠️ 未能提取当前到期日期")

        # 寻找外部续期按钮
        outer_renew_selector = None
        countdown_text = None
        possible_selectors = [
            'button:contains("续期免费套餐")',
            'type:button("Renew free plan")',
            'button:contains("Renew free plan")',
            'button:contains("续期")',
            '[class*="btn btn-primary btn-sm"]',
        ]

        for selector in possible_selectors:
            try:
                if sb.is_element_visible(selector):
                    button_text = sb.get_text(selector)
                    # "Renew in HH:MM:SS" / "Renewal cooldown HH:MM:SS" / 中文冷却 / "HH:MM:SS 后可续期" 都表示冷却中, 时间为距可续期的剩余时长
                    match = re.search(r"(?:Renew in|Renewal cooldown|续期冷却|冷却)\s*[:：]?\s*(\d{1,2}:\d{2}:\d{2})", button_text, re.I) or re.search(r"(\d{1,2}:\d{2}:\d{2})\s*后可续期", button_text)
                    if match:
                        countdown_text = match.group(1)
                        print(f"⏳ 续期冷却中, 未到续期时间: '{button_text}'")
                        break
                    elif "Renew" in button_text or "续期" in button_text:
                        outer_renew_selector = selector
                        print(f"✅ 续期按钮可用: '{button_text}'")
                        break
            except Exception as e:
                pass

        # 点击外部续期按钮等待弹窗
        if outer_renew_selector:
            print("🔄 点击外部续期按钮，等待验证窗口...")
            try:
                sb.sleep(2)
                sb.click(outer_renew_selector)
                sb.sleep(15)  # 等待模态框加载，可能因网络因素加载慢
            except Exception as e:
                print(f"❌ 点击外部按钮失败: {e}")
                send_telegram_message(format_notification("❌ 续期失败", error="点击外部续期按钮出错"))
                return

            # 处理弹窗中的 Turnstile
            turnstile_passed = True  # 未检测到 Turnstile 时视为无需验证
            if wait_turnstile_present(sb):
                turnstile_passed = handle_turnstile(sb)
                if not turnstile_passed:
                    print("❌ Turnstile 验证失败，尝试兜底点击...")
                    try:
                        sb.cdp.click_captcha()  # 再次尝试点击 Turnstile 验证
                        sb.sleep(4)
                        turnstile_passed = bool(turnstile_token(sb))
                    except Exception as e:
                        print(f"⚠️ 兜底点击失败: {e}")
                    # sb.save_screenshot("turnstile_failed.png")
            else:
                print("ℹ️ 未检测到 Turnstile")

            if not turnstile_passed:
                print("❌ Turnstile 验证最终未通过，脚本退出")
                send_telegram_message(format_notification("❌ 续期失败", error="Turnstile 验证未通过"))
                return

            # 点击续期按钮: 先列出弹窗里所有按钮, 优先点含 "续期 4 天 / Renew for 4 days" 的那个,
            # 而不是盲点第一个 .btn-primary (可能是取消/关闭, 也可能当时还是禁用状态)
            print("⏳ 等待续期按钮可用并点击...")
            time.sleep(5)

            modal_button_clicked = False
            try:
                dialog_buttons = sb.execute_script(
                    "return Array.from(document.querySelectorAll('div[role=\"dialog\"] button')).map(b => "
                    "({text: (b.innerText || '').trim(), disabled: !!b.disabled, cls: b.className}));"
                ) or []
                print(f"🔍 弹窗按钮: {dialog_buttons}")
                # 优先: 文案含续期+4天, 其次: 文案含续期/Renew, 最后: 第一个 .btn-primary
                target_idx = None
                for i, b in enumerate(dialog_buttons):
                    t = b.get("text", "") if isinstance(b, dict) else ""
                    if ("续期" in t or "Renew" in t) and ("4" in t or "天" in t or "day" in t.lower()):
                        target_idx = i
                        break
                if target_idx is None:
                    for i, b in enumerate(dialog_buttons):
                        t = b.get("text", "") if isinstance(b, dict) else ""
                        if "续期" in t or "Renew" in t:
                            target_idx = i
                            break
                if target_idx is not None:
                    _t = dialog_buttons[target_idx].get('text')
                    print(f"🖱️ 点击弹窗按钮: '{_t}'")
                    # 先用真实点击 (Selenium 点击, 会触发 Svelte/React 的事件),
                    # JS 的 btn.click() 有时只触发 DOM 事件, 按钮绑定的提交逻辑没跑起来,
                    # 这就是上次"点了但到期没变"最可能的原因; 真实点击失败才回落到 JS 点击
                    _real_clicked = False
                    try:
                        sb.click(f'div[role="dialog"] button:contains("{_t}")', timeout=8)
                        _real_clicked = True
                    except Exception as e:
                        print(f"⚠️ 真实点击失败, 改用 JS 点击: {e}")
                    if not _real_clicked:
                        sb.execute_script(
                            "var btns = document.querySelectorAll('div[role=\"dialog\"] button');"
                            f"btns[{target_idx}].click();"
                        )
                    modal_button_clicked = True
                else:
                    selector = 'div[role="dialog"] button.btn-primary'
                    sb.click(selector, timeout=8)
                    modal_button_clicked = True
                print("✅ 已点击续期按钮")
                # 点完立刻把弹窗里的文字打出来: 成功/失败提示都在这里,
                # 下次日志里能直接看到是"已续期"还是"续期失败/请重试", 不用再靠猜
                try:
                    sb.sleep(2)
                    _dlg = sb.get_text('div[role="dialog"]')
                    print(f"🔍 点后弹窗文本: {(_dlg or '').strip()[:500]}")
                except Exception as e:
                    print(f"🔍 点后弹窗已关闭或读不到文本: {e}")
                    _dlg = ""
                # v4: 点完弹窗还在、按钮还在、又没出现成功提示, 说明第一次点击没提交成功, 再点一次
                # (上次就是点了但到期没变, 很可能就是这一步丢了)
                try:
                    if _dlg and ("续期 4 天" in _dlg or "Renew for 4 days" in _dlg) and "续期成功" not in _dlg and "已续期" not in _dlg:
                        print("⚠️ 点后弹窗还在, 按钮没消失, 再点一次 '续期 4 天'")
                        sb.sleep(3)
                        sb.click(f'div[role="dialog"] button:contains("续期 4 天")', timeout=8)
                        print("✅ 已第二次点击续期按钮")
                        sb.sleep(2)
                except Exception as e:
                    print(f"⚠️ 第二次点击失败/弹窗已关闭: {e}")
            except Exception as e:
                print(f"续期按钮点击失败: {e}")

            # 点完先别立刻刷新: 轮询 90 秒等日期变化 / 成功提示, 刷新会打断还没完成的续期请求
            # 上次只等了 30 秒就刷新, 后台可能还没处理完; 另外弹窗文案里同时有
            # "当前到期 11月20日"和"续到 11月22日", 直接取第一个日期会误判为没变,
            # 所以这里也按"今天+4天"那个目标日期来判断
            try:
                _today = datetime.now(timezone(timedelta(hours=8))).date()
                _expected = (_today + timedelta(days=4)).strftime("%Y/%m/%d")
            except Exception:
                _expected = ""
            if _expected:
                print(f"📅 预期新到期日期 (今天+4天): {_expected}")
            print("⏳ 等待续期生效 (最多 90 秒, 不立即刷新)...")
            renewed_without_refresh = False
            for _ in range(18):
                sb.sleep(5)
                try:
                    check_text = sb.get_page_text()
                except Exception:
                    check_text = ""
                if any(k in check_text for k in ["续期成功", "已续期", "Renewed", "successfully renewed", "Success"]):
                    print(f"✅ 页面出现成功提示")
                    renewed_without_refresh = True
                    break
                if any(k in check_text for k in ["续期失败", "验证失败", "请重试", "Something went wrong", "failed to renew"]):
                    print(f"❌ 页面出现失败提示, 弹窗/页面文本前 500 字: {(check_text or '')[:500]}")
                    break
                if _expected and _expected in check_text:
                    print(f"📅 未刷新就检测到目标到期日期: {_expected}")
                    renewed_without_refresh = True
                    break
                try:
                    _ex = get_expiry_from_dom(sb)
                except Exception:
                    _ex = None
                if _ex and _ex != current_expiry:
                    print(f"📅 未刷新就检测到新到期日期: {_ex}")
                    renewed_without_refresh = True
                    break
            try:
                sb.save_screenshot("after_renew.png")
                with open("after_renew.html", "w", encoding="utf-8") as f:
                    f.write(sb.get_page_source())
                print("💾 已保存 after_renew.png / after_renew.html, 可用来看弹窗到底点了什么")
            except Exception as e:
                print(f"⚠️ 保存续期后快照失败: {e}")

            if not renewed_without_refresh:
                sb.sleep(3)
                sb.refresh()
            print("⏳ 等待新的过期时间...")
            sb.wait_for_ready_state_complete()
            sb.sleep(15)
            try:
                _dom_txt = sb.get_text('span.st-meta') if sb.is_element_present('span.st-meta') else ""
                print(f"🔍 刷新后账单页到期文本: {(_dom_txt or '').strip()}")
            except Exception as e:
                print(f"🔍 刷新后读到期文本失败: {e}")

            # 提取新的到期日期和倒计时
            try:
                new_page_text = sb.get_page_text() + "\n" + sb.get_page_source()
            except Exception:
                new_page_text = sb.get_page_source()
            new_expiry = get_expiry_from_dom(sb) or extract_expiry_date(new_page_text)
            new_match = re.search(r"(?:Renewal cooldown|Renew in|续期冷却|冷却)\s*[:：]?\s*(\d{1,2}:\d{2}:\d{2})", new_page_text)
            if new_match:
                new_countdown = new_match.group(1)
                print(f"✅ 续期成功！新的倒计时: {new_countdown}")
                if new_expiry:
                    print(f"📅 新的到期日期: {new_expiry}")
                send_telegram_message(
                    format_notification(
                        "✅ 续期成功",
                        extra=f"⏱️ 可续期时间: {format_countdown(new_countdown)}后",
                        expiry_date=new_expiry or "（未获取到）"
                    )
                )
            else:
                if new_expiry and new_expiry != current_expiry:
                    print(f"✅ 续期成功，到期日期已更新为: {new_expiry}")
                    send_telegram_message(
                        format_notification(
                            "✅ 续期成功",
                            extra="到期日期已更新",
                            expiry_date=new_expiry
                        )
                    )
                else:
                    # v4: 第一次刷新后没变, 别急着判失败, 后台可能还在处理:
                    # 再等 60 秒、再刷新一次, 只有两次都没变才报"可能未成功"
                    print("⏳ 刷新后到期未变, 再等 60 秒复查一次 (后台可能还在处理)...")
                    sb.sleep(60)
                    sb.refresh()
                    sb.wait_for_ready_state_complete()
                    sb.sleep(10)
                    try:
                        _txt2 = sb.get_page_text() + "\n" + sb.get_page_source()
                    except Exception:
                        _txt2 = sb.get_page_source()
                    _ex2 = get_expiry_from_dom(sb) or extract_expiry_date(_txt2)
                    if _ex2 and _ex2 != current_expiry:
                        print(f"✅ 复查续期成功, 到期日期已更新为: {_ex2}")
                        send_telegram_message(
                            format_notification(
                                "✅ 续期成功",
                                extra="到期日期已更新 (复查确认)",
                                expiry_date=_ex2
                            )
                        )
                    else:
                        print("⚠️ 续期结果未知, 两次复查到期日期都未变化, 请手动检查")
                        send_telegram_message(
                            format_notification(
                                "⚠️ 续期可能未成功",
                                extra="请登录后台检查",
                                expiry_date=current_expiry or "（未获取到）"
                            )
                        )

        else:
            if countdown_text:
                friendly = format_countdown(countdown_text)
                print(f"⏳ 未到续期时间，倒计时: {countdown_text} ({friendly})")
                send_telegram_message(
                    format_notification(
                        "⏳ 未到续期时间",
                        extra=f"⏱️ 可续期时间: {friendly}后",
                        expiry_date=current_expiry or "（未获取到）"
                    )
                )
            else:
                print("ℹ️ 未找到续期按钮或倒计时，状态未知")
                send_telegram_message(
                    format_notification(
                        "ℹ️ 无需续期",
                        extra="当前状态未知，请手动检查",
                        expiry_date=current_expiry or "（未获取到）"
                    )
                )

        print("🏁 脚本执行完毕")

if __name__ == "__main__":
    main()
