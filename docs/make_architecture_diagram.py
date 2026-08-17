"""Generate the McKesson Deployment Tool architecture diagram as a PNG."""

from PIL import Image, ImageDraw, ImageFont

W, H = 2600, 2040
BG      = "#0d1218"
PANEL   = "#171d26"
PANEL2  = "#1e2632"
BORDER  = "#2f3a48"
TEXT    = "#e6ecf3"
MUTED   = "#8b98a8"
DIM     = "#5f6c7c"
ACCENT  = "#2f7fd8"
OK      = "#3fb950"
WARN    = "#d29922"
ERR     = "#f05a5a"
VIOLET  = "#b48ce8"
TEAL    = "#4fb8b0"

F = "C:/Windows/Fonts/"
h1    = ImageFont.truetype(F + "segoeuib.ttf", 48)
h2    = ImageFont.truetype(F + "seguisb.ttf", 29)
h3    = ImageFont.truetype(F + "seguisb.ttf", 23)
body  = ImageFont.truetype(F + "segoeui.ttf", 21)
small = ImageFont.truetype(F + "segoeui.ttf", 19)
eyebrow = ImageFont.truetype(F + "seguisb.ttf", 17)
mono  = ImageFont.truetype(F + "consola.ttf", 20)
monob = ImageFont.truetype(F + "consolab.ttf", 20)
monos = ImageFont.truetype(F + "consola.ttf", 18)

img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)


def box(x0, y0, x1, y1, fill=PANEL, outline=BORDER, w=2, r=12, dash=False):
    if dash:
        d.rounded_rectangle([x0, y0, x1, y1], radius=r, fill=fill, outline=None)
        step, on = 14, 8
        for x in range(x0 + r, x1 - r, step):
            d.line([x, y0, min(x + on, x1 - r), y0], fill=outline, width=w)
            d.line([x, y1, min(x + on, x1 - r), y1], fill=outline, width=w)
        for y in range(y0 + r, y1 - r, step):
            d.line([x0, y, x0, min(y + on, y1 - r)], fill=outline, width=w)
            d.line([x1, y, x1, min(y + on, y1 - r)], fill=outline, width=w)
    else:
        d.rounded_rectangle([x0, y0, x1, y1], radius=r, fill=fill, outline=outline, width=w)


def t(x, y, s, font=body, fill=TEXT, anchor="la"):
    d.text((x, y), s, font=font, fill=fill, anchor=anchor)


def spaced(x, y, s, font=eyebrow, fill=MUTED):
    cx = x
    for ch in s:
        d.text((cx, y), ch, font=font, fill=fill)
        cx += d.textlength(ch, font=font) + 2.2


def arrow(x0, y0, x1, y1, color=ACCENT, w=3, head=13):
    d.line([x0, y0, x1, y1], fill=color, width=w)
    if y1 > y0:
        d.polygon([(x1, y1), (x1 - head, y1 - head * 1.4), (x1 + head, y1 - head * 1.4)], fill=color)
    elif y1 < y0:
        d.polygon([(x1, y1), (x1 - head, y1 + head * 1.4), (x1 + head, y1 + head * 1.4)], fill=color)
    elif x1 > x0:
        d.polygon([(x1, y1), (x1 - head * 1.4, y1 - head), (x1 - head * 1.4, y1 + head)], fill=color)


def stripe(x, y0, y1, color, w=5):
    d.rounded_rectangle([x, y0, x + w, y1], radius=3, fill=color)


# ---------------------------------------------------------------- header
t(60, 46, "McKesson Deployment Tool", h1)
t(62, 108, "Architecture and the Linux commands it runs over SSH", body, MUTED)
d.line([60, 152, W - 60, 152], fill=BORDER, width=2)

LX0, LX1 = 60, 1480
RX0, RX1 = 1545, W - 60
d.line([RX0 - 33, 176, RX0 - 33, H - 60], fill=BORDER, width=2)

# ---------------------------------------------------------------- VDI container
vdi_y0, vdi_y1 = 200, 1116
box(LX0, vdi_y0, LX1, vdi_y1, fill="#11161d", outline=DIM, w=2, r=16, dash=True)
spaced(LX0 + 26, vdi_y0 + 20, "MCKESSON VDI LAPTOP")
t(LX0 + 26, vdi_y0 + 46, "everything below runs locally  ·  nothing is exposed to the network", small, DIM)

b_y0, b_y1 = 288, 410
box(LX0 + 40, b_y0, 790, b_y1, PANEL2)
stripe(LX0 + 40, b_y0, b_y1, ACCENT)
t(LX0 + 70, b_y0 + 20, "Browser", h3)
t(LX0 + 70, b_y0 + 54, "localhost:5002", mono, ACCENT)
t(LX0 + 70, b_y0 + 84, "index.html · style.css · app.js", monos, MUTED)

box(840, b_y0, LX1 - 40, b_y1, PANEL2)
stripe(840, b_y0, b_y1, TEAL)
t(870, b_y0 + 20, "Installation hub", h3)
t(870, b_y0 + 54, "GET ?filename=&code=", mono, TEAL)
t(870, b_y0 + 84, "demo server, or local stand-in", monos, MUTED)

api_y0 = 500
arrow(300, b_y1 + 6, 300, api_y0 - 8)
t(318, b_y1 + 26, "fetch()  REST", monos, MUTED)
arrow(560, api_y0 - 8, 560, b_y1 + 6, VIOLET)
t(578, b_y1 + 26, "WebSocket  /ws/logs", monos, VIOLET)
arrow(1130, b_y1 + 6, 1130, api_y0 - 8, TEAL)
t(1148, b_y1 + 26, "JAR download", monos, TEAL)

r1 = (600, 696)
r2 = (714, 936)
r3 = (954, 1064)
api_y1 = 1086
box(LX0 + 40, api_y0, LX1 - 40, api_y1, PANEL)
t(LX0 + 70, api_y0 + 20, "FastAPI  ·  uvicorn", h2)
t(LX0 + 70, api_y0 + 58, "serves the dashboard and drives the deployment", small, MUTED)

sub_x0, sub_x1 = LX0 + 70, LX1 - 70
box(sub_x0, r1[0], sub_x1, r1[1], PANEL2, BORDER, 1, 10)
stripe(sub_x0, r1[0], r1[1], ACCENT, 4)
t(sub_x0 + 26, r1[0] + 16, "routes", h3)
t(sub_x0 + 26, r1[0] + 52, "deployment.py   ·   websocket.py", monos, MUTED)

box(sub_x0, r2[0], sub_x1, r2[1], PANEL2, BORDER, 1, 10)
stripe(sub_x0, r2[0], r2[1], VIOLET, 4)
t(sub_x0 + 26, r2[0] + 16, "services", h3)
t(sub_x0 + 26, r2[0] + 52, "deployment_service   —   orchestrates 11 stages", monos, TEXT)
t(sub_x0 + 26, r2[0] + 82, "download   sftp   backup   checksum   log", monos, MUTED)
t(sub_x0 + 26, r2[0] + 118, "allowlists: service · unit · jar · port · pid", monos, DIM)
t(sub_x0 + 26, r2[0] + 148, "backup before change  ·  verify after write", monos, DIM)
t(sub_x0 + 26, r2[0] + 178, "any stage fails  →  deployment stops", monos, WARN)

box(sub_x0, r3[0], sub_x1, r3[1], PANEL2, ACCENT, 2, 10)
t(sub_x0 + 26, r3[0] + 14, "ssh_service", h3)
t(sub_x0 + 26, r3[0] + 48, "the only place a remote command runs", monos, ACCENT)
t(sub_x0 + 26, r3[0] + 76, "argv lists via shlex.join  ·  no shell string is ever built", monos, MUTED)

# ---------------------------------------------------------------- isolation
iso_y0, iso_y1 = 1150, 1272
box(LX0, iso_y0, LX1, iso_y1, "#1a1417", ERR, 2, 14, dash=True)
t(LX0 + 30, iso_y0 + 20, "No connection to the Aiden laptop", h3, ERR)
t(LX0 + 30, iso_y0 + 58, "The checksum is copied by hand and pasted in. That is the only link between the two.",
  small, MUTED)

# ---------------------------------------------------------------- to server
arrow(770, iso_y1 + 28, 770, 1392, ACCENT, 4, 15)
t(795, iso_y1 + 52, "SSH  :22", monob, ACCENT)
t(795, iso_y1 + 80, "SFTP", monos, MUTED)

srv_y0, srv_y1 = 1402, H - 50
box(LX0, srv_y0, LX1, srv_y1, PANEL, OK, 2, 16)
stripe(LX0, srv_y0, srv_y1, OK)
t(LX0 + 34, srv_y0 + 22, "McKesson app server", h2)
t(LX0 + 34, srv_y0 + 62, "vm-mms-cims02.na.corp.mckesson.com   ·   10.15.128.5   ·   day6sio", mono, OK)

paths = [
    ("/home/day6sio/CopyData/Aug15/", "staging  —  the WinSCP step"),
    ("/home/AidenAI/binaries/", "deployed JAR  +  backups/Aug15/"),
    ("/etc/systemd/system/", "unit file  ·  APP_CHECKSUM"),
    ("/var/www/webdav/", "the application's own log file"),
]
py = srv_y0 + 118
for path, note in paths:
    t(LX0 + 34, py, path, mono, TEXT)
    t(LX0 + 34 + 430, py + 1, note, small, MUTED)
    py += 40

d.line([LX0 + 34, py + 22, LX1 - 34, py + 22], fill=BORDER, width=1)
fy = py + 56
spaced(LX0 + 34, fy, "WHAT THE DEPLOYMENT MOVES")

fy += 42
bw, gap = 400, 62
fx = LX0 + 34
flow = [
    ("CopyData/Aug15/", "<jar>   staged + verified", ACCENT),
    ("binaries/", "<jar>   copied + verified", WARN),
    ("systemd", "service restarted, checked", OK),
]
for i, (title, sub, col) in enumerate(flow):
    x0 = fx + i * (bw + gap)
    box(x0, fy, x0 + bw, fy + 84, PANEL2, col, 1, 10)
    t(x0 + 20, fy + 14, title, mono, col)
    t(x0 + 20, fy + 46, sub, monos, MUTED)
    if i < 2:
        arrow(x0 + bw + 8, fy + 42, x0 + bw + gap - 8, fy + 42, col, 3, 10)

t(fx + bw + gap / 2, fy + 106, "sudo cp", monos, DIM, anchor="ma")
t(fx + 2 * bw + gap + gap / 2, fy + 106, "sudo systemctl restart", monos, DIM, anchor="ma")

# ---------------------------------------------------------------- right column
t(RX0, 200, "Commands executed over SSH", h2)
t(RX0, 244, "every stage, in order  ·  sudo shown where it is used", small, MUTED)

y = 306
STAGE_GAP, LINE = 15, 27


def stage(num, name, cmds, color=ACCENT, note=None):
    global y
    d.rounded_rectangle([RX0, y - 4, RX0 + 40, y + 26], radius=7, fill=color)
    t(RX0 + 20, y + 11, str(num), monob, "#0d1218", anchor="mm")
    t(RX0 + 56, y - 2, name, h3)
    y += 36
    if note:
        t(RX0 + 56, y - 4, note, small, DIM)
        y += 26
    for c in cmds:
        t(RX0 + 56, y, c, monos, MUTED if c.startswith("#") else TEXT)
        y += LINE
    y += STAGE_GAP


stage(1, "Validate", ["# no SSH — allowlists checked locally"], VIOLET)
stage(2, "Download", ["# no SSH — HTTP from the installation hub"], TEAL)
stage(3, "Connect", ["id -un"], ACCENT)
stage(4, "Upload to CopyData", [
    "# SFTP, not shell",
    "stat  /home/day6sio/CopyData/Aug15",
    "mkdir /home/day6sio/CopyData/Aug15",
    "put   <jar>.part",
    "stat  <jar>.part          → verify size",
    "posix_rename <jar>.part → <jar>",
    "stat  <jar>               → verify again",
], ACCENT)
stage(5, "Backup", [
    "sudo mkdir -p .../backups/Aug15",
    "test -e .../binaries/<jar>",
    "sudo cp -p .../binaries/<jar>  .../backups/Aug15/",
    "sudo cp -p /etc/systemd/system/<unit>  .../Aug15/",
], OK)
stage(6, "Update checksum", [
    "cat  /etc/systemd/system/<unit>",
    "sudo tee /etc/systemd/system/<unit>   ← stdin",
    "cat  /etc/systemd/system/<unit>       → verify",
], WARN)
stage(7, "Daemon reload", ["sudo systemctl daemon-reload"], WARN)
stage(8, "Copy to binaries", [
    "sudo mkdir -p /home/AidenAI/binaries",
    "sudo cp .../CopyData/Aug15/<jar>  .../binaries/<jar>",
    "sudo stat -c %s .../binaries/<jar>    → verify size",
], WARN)
stage(9, "Restart", ["sudo systemctl restart <unit>"], ERR)
stage(10, "Health check", [
    "sudo systemctl is-active <unit>",
    "sudo systemctl status <unit> --no-pager",
    "sudo ls -1 /var/www/webdav",
], OK)
stage(11, "Live logs", [
    "sudo journalctl -u <unit> -n 200 -f --no-pager",
    "sudo tail -n 200 -F /var/www/webdav/<service>.log",
], VIOLET)

y += 4
d.line([RX0, y, RX1, y], fill=BORDER, width=2)
y += 26
t(RX0, y, "Only on failure", h3, ERR)
y += 40
for c in [
    "sudo journalctl -u <unit> -n 60 --no-pager",
    "sudo tail -n 60 /var/www/webdav/<service>.log",
    "sudo lsof -i :9091 -P -n",
]:
    t(RX0 + 56, y, c, monos, TEXT)
    y += LINE

y += 22
t(RX0, y, "Only after you confirm", h3, ERR)
y += 40
for c in [
    "sudo cat /proc/<pid>/comm",
    "sudo kill <pid>",
    "sudo test -d /proc/<pid>              → verify exit",
]:
    t(RX0 + 56, y, c, monos, TEXT)
    y += LINE

y += 26
t(RX0, y, "Nothing else is ever run.  No shell is exposed.", small, DIM)

img.save("architecture.png")
print(f"written {W}x{H}   right column ends at y={y}")
