# Offline zh->en sample outputs (2026-10-30)

Machine: Windows 11, 32 logical CPUs, CPython 3.12.16 in a throwaway venv. Scripts: `docs/research/scratch/bench_argos_zh_en.py`, `docs/research/scratch/bench_quickmt_cpu.py`.
16 hand-written casual chat / UI lines (no reference translations; quality is my informal judgement, not a metric).

| zh | Argos zh-en 1.9 (what LibreTranslate uses) | quickmt-zh-en, CT2 int8, beam 2 |
|---|---|---|
| 哈哈哈你也太搞笑了吧 | You're so funny. | You are funny too. |
| 老板大气!刷个火箭支持一下 | Boss atmosphere! (second half dropped) | Give me a rocket support! |
| 今天不开播了，晚安宝子们 | It's closed today. Good night, boys. | It's off today, good night treasures. |
| 这个单怎么接？要先付款吗 | How do I get this? Do I have to pay first? | How do I get this order? Pay first? |
| 我先去打团，等我五分钟 | I'll start with the group. Give me five minutes. | I'll wait five minutes. (wrong) |
| 主播好帅啊，求带飞 | The anchor is so handsome. (second half dropped) | Good host, please fly. |
| 你是不是在逗我，笑死 | Are you kidding me? | Are you laughing at me? |
| 已下单，请查收 | Please check in. (wrong) | Please check |
| 666 太强了 | 666, too strong. | 666 is too strong. |
| 别划水了，快来开黑 | No more rowing, no more blacking. (wrong) | Stop the water. Come on. (wrong) |
| 这家伙太菜了，又躺赢 | This guy's too good. He's back on the ground again. (wrong) | This guy is too dishy and lays to win again (wrong) |
| 姐姐能加个微信吗 | Can I add a word? (wrong) | Can I add a WeChat? |
| 充值入口在哪里 | Where's the loading entrance? | Where is the top-up entry? |
| 接单大厅 | The pick-up hall. | Pickup Hall |
| 我的订单 | My orders. | My order |
| 设置 | Settings | Set up |

Timing (one line per call, CPU, 4 intra threads for quickmt, after warm-up):
- Argos: about 23 ms/line (whole 16-line loop 0.4 s; model load not separated, whole script 1.2 s incl. imports).
- quickmt: 35 ms/line (default, float16 weights upcast to float32) and 18 ms/line (int8 on the fly).
Sizes: Argos zh_en package download 74.5 MB. quickmt `model.bin` 407 MB (float16) plus two 0.7-0.8 MB SentencePiece models. Memory (RSS) not measured.
LibreTranslate 1.9.6 pip venv: 429 MB total; no torch, stanza or spacy present (ctranslate2 62 MB, onnxruntime 48 MB, pymupdf 105 MB).
