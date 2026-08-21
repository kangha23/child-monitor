# Child Monitor — Development & AI Agent Guidelines

Tài liệu này định nghĩa các quy tắc cốt lõi (Rules) bắt buộc mọi AI/Agent phải tuân thủ khi chỉnh sửa hoặc phát triển mã nguồn của dự án này.

---

## 1. Quy tắc Xử lý Lệnh Telegram (`command_listener` & `handle_command`)
- **Luôn cắt bỏ hậu tố `@bot_username`**: Khi người dùng dùng bot trong Group Telegram, lệnh gửi về sẽ có dạng `/lock@Monitor239_bot`. Bắt buộc phải xử lý:
  ```python
  cmd = parts[0].strip().lower().split("@")[0]
  ```
- **Hỗ trợ đa dạng Chat ID**: Không so khớp cứng nhắc một định dạng. Phải hỗ trợ cả Chat riêng, Chat nhóm thông thường và Supergroup (có tiền tố `-100...`):
  ```python
  chat_id == cfg_chat or chat_id.replace("-100", "-") == cfg_chat.replace("-100", "-")
  ```
- **Xử lý toàn bộ loại Message Object**: Luôn kiểm tra `result.get("message") or result.get("channel_post") or result.get("edited_message")`.
- **Danh sách lệnh bắt buộc duy trì**:
  - `🔒 /lock` — Khóa màn hình (`user32.LockWorkStation`).
  - `📸 /screen`, `/screenshot` — Chụp màn hình gửi Telegram.
  - `📷 /camera` — Chụp ảnh webcam gửi Telegram.
  - `📊 /report` — Báo cáo nhanh ứng dụng và phím bấm.
  - `🌐 /remote`, `/stopremote` — Bật/Tắt WebRTC H.264 Remote Desktop.
  - `🔄 /update` — Tự động cập nhật code nóng từ xa qua GitHub.
  - `⚡ /status`, `/cmd`, `/restart`, `/shutdown`, `/start`, `/stop`, `/help`.

---

## 2. Quy tắc Tính Năng Cập Nhật Từ Xa (Remote Dynamic Update)
- **Không bao giờ xóa bỏ cơ chế Update**:
  - Tuyệt đối không xóa các hàm `fetch_remote_update()`, `run_runtime_code()`, `restart_process()`, `remote_update_cmd()` và lệnh `/update`.
  - Giữ nguyên đoạn nạp code động từ thư mục `runtime/` trong hàm `main()`.
- **Quy tắc Nâng Version (Version Bumping)**:
  - Mỗi khi thêm tính năng mới, sửa lỗi hoặc thay đổi `monitor.py`, **BẮT BUỘC** phải nâng số phiên bản trong file `version.txt` (ví dụ: `1.0.3` -> `1.0.4`).
  - Đảm bảo `version.txt` và `monitor.py` luôn đồng bộ để khi người dùng gõ `/update`, bot phát hiện được phiên bản mới trên GitHub.

---

## 3. Quy tắc Bảo Toàn Tính Năng Giám Sát & WebRTC Remote
- Khi tối ưu hoặc thêm tính năng mới, **KHÔNG ĐƯỢC XÓA** các module nền hiện có:
  - `window_poller` (Ghi nhận ứng dụng đang sử dụng)
  - `keylog_thread` (Ghi nhận phím gõ & tái tạo từ ngữ)
  - `screenshot_thread` & `camera_thread` (Chụp ảnh định kỳ)
  - `reporter_thread` (Gửi báo cáo định kỳ)
  - `startup_notice` & `self_install` (Tự khởi động cùng Windows)
  - `WebRTC H.264 Engine` & `EmbeddedViewerServer` (Port 8088 phục vụ Web Remote)

---

## 4. Kiểm tra trước khi bàn giao (Verification)
- Luôn kiểm tra cú pháp bằng lệnh: `python -m py_compile monitor.py`.
- Đảm bảo không bị thiếu dependencies trong `requirements.txt`.
