import tkinter as tk
from PIL import Image, ImageTk, ImageFilter, ImageDraw, ImageFont
import asyncio
import io
import sys
import threading
import time

# 修正: コード全体に含まれていた不正な空白文字（ノーブレークスペース）を通常の半角スペースに一括変換しました。
# これにより SyntaxError が解消され、正常に実行できるようになります。

# ライブラリの読み込み
try:
    from winsdk.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as MediaManager
    from winsdk.windows.storage.streams import DataReader
except ImportError:
    print("Error: 'winsdk' is required. Run: pip install winsdk")
    sys.exit(1)

class SpotifyWidget:
    def __init__(self, root):
        self.root = root
        self.root.title("Spotify Now Playing")
        self.width, self.height = 320, 520
        self.root.geometry(f"{self.width}x{self.height}")
        self.root.resizable(False, False)
        
        self.root.after(10, lambda: self.root.overrideredirect(True))
        self.root.attributes("-topmost", True)

        self.transparent_color = "#abcdef" 
        self.root.configure(bg=self.transparent_color)
        self.root.attributes("-transparentcolor", self.transparent_color)

        self._drag_data = {"x": 0, "y": 0}
        self.root.bind("<Button-1>", self.start_move)
        self.root.bind("<B1-Motion>", self.do_move)

        self.canvas = tk.Canvas(root, width=self.width, height=self.height, bg=self.transparent_color, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self.main_image_item = self.canvas.create_image(160, 260)

        self.close_btn_size, self.close_btn_x, self.close_btn_y = 24, 285, 15
        self.close_hover = False
        self.controls_y, self.btn_radius = 475, 22
        self.btns = {
            "prev": {"x": 100, "hover": False},
            "play": {"x": 160, "hover": False},
            "next": {"x": 220, "hover": False}
        }

        # --- フォント設定 ---
        font_path_bold = "C:/Windows/Fonts/meiryob.ttc"
        font_path_reg = "C:/Windows/Fonts/meiryo.ttc"
        try:
            self.font_title = ImageFont.truetype(font_path_bold, 18)
            self.font_artist = ImageFont.truetype(font_path_reg, 16) # 修正: アーティスト名のサイズを14から16に大きくしました
            self.font_time = ImageFont.truetype(font_path_reg, 13) # 修正: 再生時間のサイズを11から13に大きくしました
        except:
            self.font_title = self.font_artist = self.font_time = ImageFont.load_default()

        self.last_track_id = ""
        self.is_running = True
        self.current_data = None
        self.cached_bg = None
        self.cached_album = None
        
        # --- 時間管理用の変数 ---
        self.last_sync_pos = 0.0
        self.last_sync_time = time.perf_counter()
        
        self.tk_img = None 

        self.canvas.bind("<Motion>", self.on_canvas_motion)
        self.canvas.bind("<Button-1>", self.on_canvas_click)
        self.canvas.bind("<Leave>", self.on_canvas_leave)

        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.start_async_loop, daemon=True).start()

        self.fetch_data_loop()
        self.draw_loop()

    def start_async_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def start_move(self, event):
        self._drag_data["x"], self._drag_data["y"] = event.x, event.y

    def do_move(self, event):
        if self.is_in_close_btn(self._drag_data["x"], self._drag_data["y"]) or self.get_btn_at(self._drag_data["x"], self._drag_data["y"]): return
        x = self.root.winfo_x() + (event.x - self._drag_data["x"])
        y = self.root.winfo_y() + (event.y - self._drag_data["y"])
        self.root.geometry(f"+{x}+{y}")

    def is_in_close_btn(self, x, y):
        return self.close_btn_x <= x <= self.close_btn_x + self.close_btn_size and self.close_btn_y <= y <= self.close_btn_y + self.close_btn_size

    def get_btn_at(self, x, y):
        for name, info in self.btns.items():
            if (x - info["x"])**2 + (y - self.controls_y)**2 <= self.btn_radius**2: return name
        return None

    def on_canvas_motion(self, event):
        in_close, btn_name = self.is_in_close_btn(event.x, event.y), self.get_btn_at(event.x, event.y)
        self.close_hover = in_close
        for name in self.btns: self.btns[name]["hover"] = (name == btn_name)
        self.canvas.config(cursor="hand2" if (in_close or btn_name) else "")

    def on_canvas_leave(self, event):
        self.close_hover = False
        for name in self.btns: self.btns[name]["hover"] = False

    def on_canvas_click(self, event):
        if self.is_in_close_btn(event.x, event.y): self.is_running = False; self.root.destroy(); return
        btn = self.get_btn_at(event.x, event.y)
        if btn: asyncio.run_coroutine_threadsafe(self.send_media_command(btn), self.loop)

    async def send_media_command(self, cmd):
        sessions = await MediaManager.request_async()
        curr = sessions.get_current_session()
        if not curr: return
        try:
            if cmd == "play":
                status = curr.get_playback_info().playback_status
                await (curr.try_pause_async() if status == 4 else curr.try_play_async())
            elif cmd == "prev": await curr.try_skip_previous_async()
            elif cmd == "next": await curr.try_skip_next_async()
        except: pass

    def fetch_data_loop(self):
        if not self.is_running or not self.root.winfo_exists(): return
        future = asyncio.run_coroutine_threadsafe(self.fetch_media_info(), self.loop)
        future.add_done_callback(lambda f: self.root.after(0, lambda: self.on_data_fetched(f.result())))

    def on_data_fetched(self, data):
        if not self.is_running or not self.root.winfo_exists(): return
        if data:
            is_track_changed = data['id'] != self.last_track_id
            is_status_changed = self.current_data and data['status'] != self.current_data['status']
            
            # 現在の自前予測位置を算出
            if self.current_data and self.current_data['status'] == 4:
                elapsed = time.perf_counter() - self.last_sync_time
                estimated_pos = self.last_sync_pos + elapsed
            else:
                estimated_pos = self.last_sync_pos
                
            # # 修正: 巻き戻し判定のロジックを変更し、ロールバックを完全に防止
            is_seeked = False
            if self.current_data:
                # OSのタイムライン更新が遅れた場合、data['pos']は「過去のまま」になります。
                # 予測位置(estimated_pos)と比較すると巻き戻ったと誤判定してしまうため、
                # 「前回取得したOSの生の位置データ(self.current_data['pos'])」と比較し、値が減少した時だけ巻き戻しと判定します。
                is_seeked_backward = data['pos'] < (self.current_data['pos'] - 1.0)
                # 早送り判定はそのまま予測位置との比較でOK（OSの値が未来に飛ぶのはシーク時のみのため）
                is_seeked_forward = data['pos'] > (estimated_pos + 2.0)
                is_seeked = is_seeked_backward or is_seeked_forward
            
            self.current_data = data
            
            # トラック変更、ステータス変更、またはシーク時のみ時間を同期する
            if is_track_changed or is_status_changed or is_seeked or self.last_sync_pos == 0.0:
                self.last_sync_pos = data['pos']
                self.last_sync_time = time.perf_counter()
                
        self.root.after(800, self.fetch_data_loop)

    async def fetch_media_info(self):
        try:
            sessions = await MediaManager.request_async()
            current = sessions.get_current_session()
            if not current: return None
            props = await current.try_get_media_properties_async()
            timeline = current.get_timeline_properties()
            
            image = None
            track_id = f"{props.title}{props.artist}"
            if props.thumbnail and track_id != self.last_track_id:
                stream = await props.thumbnail.open_read_async()
                reader = DataReader(stream)
                await reader.load_async(stream.size)
                image = Image.open(io.BytesIO(bytes([reader.read_byte() for _ in range(stream.size)])))

            return {
                "title": props.title, "artist": props.artist, "image": image, "id": track_id,
                "status": current.get_playback_info().playback_status,
                "pos": timeline.position.total_seconds(), "dur": timeline.end_time.total_seconds()
            }
        except: return None

    def get_smart_wrapped_text(self, text, draw, max_width, font):
        lines, cur = [], ""
        for c in text:
            if draw.textbbox((0,0), cur+c, font=font)[2] <= max_width: cur += c
            else: lines.append(cur); cur = c
        lines.append(cur)
        if len(lines) > 2:
            res = lines[:2]
            while draw.textbbox((0,0), res[1]+"...", font=font)[2] > max_width: res[1] = res[1][:-1]
            res[1] += "..."
            return res
        return lines

    def draw_loop(self):
        if not self.is_running or not self.root.winfo_exists(): return
        if self.current_data:
            if self.current_data['status'] == 4: # 4 = Playing
                elapsed = time.perf_counter() - self.last_sync_time
                current_pos = min(self.current_data['dur'], self.last_sync_pos + elapsed)
            else:
                current_pos = self.last_sync_pos

            img = self.render_ui(self.current_data, current_pos)
            self.tk_img = ImageTk.PhotoImage(img)
            self.canvas.itemconfig(self.main_image_item, image=self.tk_img)
        
        self.root.after(30, self.draw_loop)

    def render_ui(self, data, current_pos):
        base = Image.new("RGBA", (self.width, self.height), (0,0,0,0))
        
        if data['id'] != self.last_track_id:
            if data['image']:
                self.cached_bg = data['image'].resize((self.width, self.height)).filter(ImageFilter.GaussianBlur(40)).convert("RGBA")
                mask = Image.new("L", (self.width, self.height), 0)
                ImageDraw.Draw(mask).rounded_rectangle((0,0,self.width,self.height), 30, fill=255)
                self.cached_bg.putalpha(mask)
                
                self.cached_album = data['image'].resize((240,240), Image.Resampling.LANCZOS)
                a_mask = Image.new("L", (240,240), 0)
                ImageDraw.Draw(a_mask).rounded_rectangle((0,0,240,240), 20, fill=255)
                self.cached_album.putalpha(a_mask)
            self.last_track_id = data['id']

        if self.cached_bg:
            bg = self.cached_bg.copy()
            bg.putalpha(bg.getchannel('A').point(lambda i: i * 0.5))
            base.paste(bg, (0,0), bg)

        draw = ImageDraw.Draw(base)
        draw.rounded_rectangle((0,0,self.width,self.height), 30, fill=(20,20,20,40)) 
        if self.cached_album: base.paste(self.cached_album, (40, 40), self.cached_album)

        y = 310
        title_lines = self.get_smart_wrapped_text(data['title'], draw, 260, self.font_title)
        for l in title_lines: 
            draw.text((160, y), l, font=self.font_title, fill=(255,255,255), anchor="mm")
            y += 22
        
        y += 6
        artist_lines = self.get_smart_wrapped_text(data['artist'], draw, 260, self.font_artist)
        for l in artist_lines: 
            draw.text((160, y), l, font=self.font_artist, fill=(255,255,255,180), anchor="mm")
            y += 20 # 修正: アーティスト名のフォントが大きくなったため、複数行になった時の行間を18から20に広げました

        # --- 秒数・プログレスバーの描画 ---
        ratio = current_pos / data['dur'] if data['dur'] > 0 else 0
        draw.rounded_rectangle((40, 415, 280, 418), 2, fill=(255,255,255,40))
        draw.rounded_rectangle((40, 415, 40 + 240 * ratio, 418), 2, fill=(255,255,255,220))
        
        time_str = f"{int(current_pos//60):02}:{int(current_pos%60):02} / {int(data['dur']//60):02}:{int(data['dur']%60):02}"
        draw.text((160, 435), time_str, font=self.font_time, fill=(255,255,255,140), anchor="mm")

        for name, info in self.btns.items():
            x, r = info["x"], self.btn_radius
            icon_c = (255,255,255, 240 if info["hover"] else 180)
            if info["hover"]: draw.ellipse((x-r+4, self.controls_y-r+4, x+r-4, self.controls_y+r-4), fill=(255,255,255,30))
            if name == "play":
                if data['status'] == 4:
                    draw.rectangle((x-4, self.controls_y-7, x-1, self.controls_y+7), fill=icon_c)
                    draw.rectangle((x+1, self.controls_y-7, x+4, self.controls_y+7), fill=icon_c)
                else: draw.polygon([(x-4, self.controls_y-8), (x-4, self.controls_y+8), (x+8, self.controls_y)], fill=icon_c)
            elif name == "prev":
                draw.polygon([(x+4, self.controls_y-6), (x+4, self.controls_y+6), (x-3, self.controls_y)], fill=icon_c)
                draw.rectangle((x-6, self.controls_y-6, x-4, self.controls_y+6), fill=icon_c)
            elif name == "next":
                draw.polygon([(x-4, self.controls_y-6), (x-4, self.controls_y+6), (x+3, self.controls_y)], fill=icon_c)
                draw.rectangle((x+4, self.controls_y-6, x+6, self.controls_y+6), fill=icon_c)

        cx, cy, cs = self.close_btn_x, self.close_btn_y, self.close_btn_size
        if self.close_hover: draw.rounded_rectangle((cx, cy, cx+cs, cy+cs), 4, fill=(230,50,50,160))
        draw.line((cx+8, cy+8, cx+cs-8, cy+cs-8), fill=(255,255,255,180), width=2)
        draw.line((cx+cs-8, cy+8, cx+8, cy+cs-8), fill=(255,255,255,180), width=2)

        return base

if __name__ == "__main__":
    root = tk.Tk()
    SpotifyWidget(root)
    root.mainloop()
