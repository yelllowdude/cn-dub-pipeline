# Chinese glossary — locked terms

Extracted from `docs/cn_workflow.html`'s Stage 3 table so both the doc and
`SKILL.md` point at one canonical file instead of drifting. When translating,
check every term against this list — do not re-derive a term you've already
locked here, and add any new recurring term back to this file (not just to
the individual `{id}_zh.srt`) so it's consistent on the next video too.

| Source term | Rule |
|---|---|
| "Go Bananas" | Stays in English. Always. Reads as explosive progress / beast-mode strength — the way "Just Do It" is never translated. Translate the rest of the sentence naturally around it: *"你的力量马上要 go bananas 了"*. |
| "Yellow Dude" | → 光头黄 only. (Confirmed against the channel's own live upload title — not 黄头黄.) |

## Title-suffix convention (not a glossary term, but equally locked)

Every V1/V2 title pair ends in a full-width-bracketed tag, not a suffix appended without brackets:
- V1 (英配中字): `{中文标题}（English title）【英配中字】`
- V2 (中配): `{中文标题} 【中配】`
- Alternates (backup titles): `{中文标题}【中配】`

## Ad-disclosure boilerplate (when `Contains ads?` is ticked)

`# CN ad disclosure` section, own heading — never inside the `# CN description`
code fence:
```
本视频含广告（{sponsor name}）——Bilibili上传时,在声明频道类型步骤需勾选:内容、口播、简介
```
Adjust which of 内容/口播/简介 apply to what the sponsor segment actually
contains — don't tick all three reflexively if, say, there's no verbal
mention.
