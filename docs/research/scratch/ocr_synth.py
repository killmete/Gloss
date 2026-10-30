"""Synthetic test images (NOT real screenshots) with known ground truth.
Run with the interpreter that has pillow. Returns dict name -> (PIL image, expected text)."""
from PIL import Image, ImageDraw, ImageFont
F = "C:/Windows/Fonts/"
def font(name, size, idx=0):
    return ImageFont.truetype(F + name, size, index=idx)

def snippet(text, fnt, fg, bg, pad=12, stroke=0, stroke_fill=None, grad=None, bubble=False):
    d0 = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    l, t, r, b = d0.textbbox((0, 0), text, font=fnt, stroke_width=stroke)
    w, h = r - l + 2 * pad, b - t + 2 * pad
    if grad:
        img = Image.new("RGB", (w, h))
        gd = ImageDraw.Draw(img)
        for y in range(h):
            k = y / max(h - 1, 1)
            gd.line([(0, y), (w, y)], fill=tuple(int(grad[0][i] * (1 - k) + grad[1][i] * k) for i in range(3)))
    else:
        img = Image.new("RGB", (w, h), (30, 30, 36) if bubble else bg)
        if bubble:
            ImageDraw.Draw(img).rounded_rectangle((2, 2, w - 3, h - 3), radius=10, fill=bg)
    ImageDraw.Draw(img).text((pad - l, pad - t), text, font=fnt, fill=fg, stroke_width=stroke, stroke_fill=stroke_fill)
    return img

def cases():
    yh, yhb = "msyh.ttc", "msyhbd.ttc"
    c = {}
    c["zh_chat_bubble_16"] = (snippet("小王子：今晚有空一起开黑吗？我带你上分~", font(yh, 16), (30, 30, 30), (170, 235, 120), bubble=True), "小王子：今晚有空一起开黑吗？我带你上分~")
    c["zh_chat_bubble_white_14"] = (snippet("老板好，请问需要什么服务呢？", font(yh, 14), (255, 255, 255), (60, 120, 220), bubble=True), "老板好，请问需要什么服务呢？")
    c["zh_order_card_dark_18"] = (snippet("接单成功！订单金额 ¥128.00 请及时联系老板", font(yh, 18), (255, 255, 255), (28, 30, 48)), "接单成功！订单金额 ¥128.00 请及时联系老板")
    c["zh_stylised_stroke_28"] = (snippet("恭喜获得神秘大礼包", font(yhb, 28), (255, 225, 60), None, stroke=3, stroke_fill=(120, 30, 10), grad=((90, 40, 160), (200, 80, 120))), "恭喜获得神秘大礼包")
    c["zh_tiny_grey_12"] = (snippet("距离结束还有 02:35:11 剩余名额 3", font(yh, 12), (140, 140, 140), (250, 250, 250)), "距离结束还有 02:35:11 剩余名额 3")
    c["zh_mixed_name_14"] = (snippet("Lucky_77：欢迎来到直播间，点个关注不迷路", font(yh, 14), (240, 240, 240), (40, 40, 40)), "Lucky_77：欢迎来到直播间，点个关注不迷路")
    c["zh_menu_16"] = (snippet("我的订单   优惠券   客服中心   设置", font("simsun.ttc", 16), (50, 50, 50), (255, 255, 255)), "我的订单 优惠券 客服中心 设置")
    c["ja_chat_16"] = (snippet("今日はいい天気ですね。一緒にゲームしませんか？", font("YuGothM.ttc", 16), (20, 20, 20), (235, 235, 235)), "今日はいい天気ですね。一緒にゲームしませんか？")
    c["ko_chat_16"] = (snippet("안녕하세요 오늘 같이 게임할래요?", font("malgun.ttf", 16), (20, 20, 20), (235, 235, 235)), "안녕하세요 오늘 같이 게임할래요?")
    return c

def composite(cs):
    """1280x720 fake screen with all snippets stacked, for full-frame timing."""
    img = Image.new("RGB", (1280, 720), (20, 22, 30))
    y = 20
    for name, (im, _) in cs.items():
        img.paste(im, (40 + (y % 3) * 200, y)); y += im.height + 18
    return img
