import os
import sys
import asyncio
import aiohttp
from datetime import datetime
from playwright.async_api import async_playwright

LOGIN_URL = "https://wispbyte.com/client/servers"


# ========================
# 工具函数
# ========================

def mask_email(email: str) -> str:
    """
    邮箱脱敏：
    abcdef@gmail.com → abc****@gmail.com
    """
    try:
        name, domain = email.split("@", 1)
        prefix = name[:3] if len(name) >= 3 else name
        return f"{prefix}****@{domain}"
    except Exception:
        return "****@****"


async def tg_notify(message: str):
    """
    Telegram 通知（可选）
    """
    token = os.getenv("TG_BOT_TOKEN")
    chat_id = os.getenv("TG_CHAT_ID")

    if not token or not chat_id:
        print("[INFO] 未配置 Telegram，跳过通知")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    async with aiohttp.ClientSession() as session:
        try:
            await session.post(url, data={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            })
        except Exception as e:
            print(f"[WARN] Telegram 发送失败: {e}")


def build_report(results, start_time, end_time):
    """
    构建最终通知报告（已脱敏）
    """
    success = [r for r in results if r["success"]]
    failed  = [r for r in results if not r["success"]]

    lines = [
        "Wispbyte 自动登录报告",
        f"时间: {start_time} → {end_time}",
        f"结果: {len(success)} 成功 | {len(failed)} 失败",
        ""
    ]

    if success:
        lines.append("成功账号：")
        for r in success:
            lines.append(f" - {mask_email(r['email'])}")
        lines.append("")

    if failed:
        lines.append("失败账号：")
        for r in failed:
            lines.append(f" - {mask_email(r['email'])}")

    return "\n".join(lines)


# ========================
# 单账号登录逻辑
# ========================

async def login_one(email: str, password: str):
    safe_email = mask_email(email)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-extensions",
                "--window-size=1920,1080",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
        )

        page = await context.new_page()
        page.set_default_timeout(90000)

        result = {"email": email, "success": False}
        max_retries = 2

        for attempt in range(max_retries + 1):
            try:
                print(f"[{safe_email}] 尝试 {attempt + 1}: 打开登录页")
                await page.goto(LOGIN_URL, wait_until="load", timeout=90000)
                await page.wait_for_load_state("domcontentloaded", timeout=30000)
                await asyncio.sleep(5)

                # 已登录判断
                if "client" in page.url and "login" not in page.url.lower():
                    print(f"[{safe_email}] 已处于登录状态")
                    result["success"] = True
                    break

                # 填写表单
                await page.wait_for_selector(
                    'input[placeholder*="Email"], input[placeholder*="Username"], '
                    'input[type="email"], input[type="text"]',
                    timeout=20000,
                )

                await page.fill(
                    'input[placeholder*="Email"], input[placeholder*="Username"], '
                    'input[type="email"], input[type="text"]',
                    email,
                )
                await page.fill('input[placeholder*="Password"], input[type="password"]', password)

                # 尝试简单人机验证（非强制）
                try:
                    await page.wait_for_selector(
                        'text=确认您是真人, input[type="checkbox"]',
                        timeout=8000,
                    )
                    await page.click('text=确认您是真人')
                    await asyncio.sleep(3)
                except Exception:
                    pass

                await page.click('button:has-text("Log In")')
                await page.wait_for_url("**/client**", timeout=30000)

                print(f"[{safe_email}] 登录成功")
                result["success"] = True
                break

            except Exception as e:
                print(f"[{safe_email}] 尝试失败: {e}")

                if attempt < max_retries:
                    await context.close()
                    context = await browser.new_context(
                        viewport={"width": 1920, "height": 1080},
                        user_agent=(
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/130.0.0.0 Safari/537.36"
                        ),
                    )
                    page = await context.new_page()
                    await asyncio.sleep(2)
                else:
                    # 最终失败截图（立即清理）
                    filename = f"error_{safe_email.replace('@', '_')}_{int(datetime.now().timestamp())}.png"
                    await page.screenshot(path=filename, full_page=True)
                    if os.path.exists(filename):
                        os.remove(filename)

        await context.close()
        await browser.close()
        return result


# ========================
# 主入口
# ========================

async def main():
    start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    accounts_str = os.getenv("WISP_ACCOUNTS")
    if not accounts_str:
        await tg_notify("Failed: 未配置 WISP_ACCOUNTS")
        return

    accounts = [a.strip() for a in accounts_str.split(",") if ":" in a]
    if not accounts:
        await tg_notify("Failed: WISP_ACCOUNTS 格式错误，应为 email:password")
        return

    tasks = [
        login_one(email, pwd)
        for email, pwd in (acc.split(":", 1) for acc in accounts)
    ]

    results = await asyncio.gather(*tasks)

    end_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    report = build_report(results, start_time, end_time)

    print(report)
    await tg_notify(report)


if __name__ == "__main__":
    accounts = os.getenv("WISP_ACCOUNTS", "").strip()
    count = len([a for a in accounts.split(",") if ":" in a]) if accounts else 0

    print(f"[{datetime.now()}] login.py 启动", file=sys.stderr)
    print(f"Python: {sys.version.split()[0]}, 有效账号数: {count}", file=sys.stderr)

    asyncio.run(main())
