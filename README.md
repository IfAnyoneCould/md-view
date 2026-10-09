# md-view

Markdown preview in a wezterm split, drawn the way markview drew it inside nvim. Follows nvim live as you type, and mermaid diagrams and images show as real images.

## Setup

```powershell
npm i -g @mermaid-js/mermaid-cli
```

Needs `uv`, `node`, and Edge or Chrome (mermaid renders in it headless, so mermaid-cli's own Chromium download isn't needed). `magick` for svg images.

In nvim, `lua/plugins/markdown.lua` opens it with a markdown file, `<leader>cp` toggles and `:q` takes it with you. markview stays installed but draws nothing, md-view reads its highlight groups and code block icons over the pipe.

## Usage

```sh
uv run --script ~/Projects/md-view/mdv.py --nvim <servername>   # what nvim runs
uv run --script ~/Projects/md-view/mdv.py README.md             # just a file, follows it on save
```

A file that's already open in an nvim gets followed live from that nvim instead.

In the preview: `j`/`k`, `ctrl-d`/`ctrl-u`, `g`/`G` scroll, `q` quits. Moving the cursor in nvim snaps it back.

## How it works

- `mdv.py` connects to nvim's pipe and runs some lua there that pushes the buffer, cursor and colours on every change, so it never has to ask
- markdown-it parses it, blank lines are kept where they were so the preview lines up with the source
- code blocks are highlighted with pygments using nvim's treesitter colours
- images go out with wezterm's iterm image protocol, scaled and cropped to the rows on screen. kitty graphics don't work here, conpty eats them
- `mermaid.mjs` keeps one headless browser open, a diagram takes ~400ms instead of mmdc's ~2.5s. diagrams are themed from nvim's colours and cached in `%LOCALAPPDATA%\md-view`

## Notes

- conpty sits between every windows program and wezterm and only resends lines it thinks changed, so lines an image covered get scribbled on first to force a redraw
- the cursor can't be hidden, conpty drops images while it is, so it sits in the bottom right corner
- an image never touches the last row, wezterm scrolls when one ends there and conpty doesnt know
- nvim in wsl isn't supported, its pipe is a unix socket on the other side
- `MDV_DEBUG=1` logs events to `%LOCALAPPDATA%\md-view\debug.log`, crashes go to `mdv.log`
