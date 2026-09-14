"""
PC 진단 - 원클릭 화면.
  [진단 시작] -> 수집/분석 -> 리포트 표시
  [HTML 리포트 저장] -> 파일로 저장 + 브라우저로 열기
디자인은 최소 (기능 우선). diagnostic.py 를 사용.
"""
import os
import threading
import tkinter as tk
from tkinter import filedialog, scrolledtext, messagebox

import diagnostic
import repair
import analysis
import history


class DiagnosticApp:
    def __init__(self):
        self.data = None
        self.root = tk.Tk()
        self.root.title("PC 진단")
        self.root.geometry("760x580")
        self.root.configure(bg="#1e1e2e")

        top = tk.Frame(self.root, bg="#1e1e2e")
        top.pack(fill="x", padx=12, pady=(10, 2))
        self.btn_run = tk.Button(top, text="🔍 진단 시작", command=self.run,
                                 bg="#89b4fa", fg="#11111b", relief="flat",
                                 font=("Malgun Gothic", 12, "bold"), padx=14, pady=7)
        self.btn_run.pack(side="left")
        self.btn_autofix = tk.Button(top, text="⚡ 자동 복구", command=self.auto_repair,
                                     bg="#f38ba8", fg="#11111b", relief="flat",
                                     padx=10, pady=7, state="disabled")
        self.btn_autofix.pack(side="left", padx=6)
        self.btn_save = tk.Button(top, text="📄 저장", command=self.save_html,
                                  bg="#a6e3a1", fg="#11111b", relief="flat",
                                  padx=10, pady=7, state="disabled")
        self.btn_save.pack(side="left")
        self.btn_hist = tk.Button(top, text="📋 이력", command=self.show_history,
                                  bg="#b4befe", fg="#11111b", relief="flat", padx=10, pady=7)
        self.btn_hist.pack(side="left", padx=6)

        top2 = tk.Frame(self.root, bg="#1e1e2e")
        top2.pack(fill="x", padx=12, pady=(0, 6))
        self.btn_repair = tk.Button(top2, text="🌐 인터넷 복구", command=self.repair_internet,
                                    bg="#f9e2af", fg="#11111b", relief="flat", padx=10, pady=6)
        self.btn_repair.pack(side="left")
        self.btn_winfix = tk.Button(top2, text="🛠 Windows 복구", command=self.win_repair,
                                    bg="#fab387", fg="#11111b", relief="flat", padx=10, pady=6)
        self.btn_winfix.pack(side="left", padx=6)
        self.btn_installed = tk.Button(top2, text="📦 설치 프로그램", command=self.show_installed,
                                       bg="#94e2d5", fg="#11111b", relief="flat", padx=10, pady=6)
        self.btn_installed.pack(side="left")

        self.status = tk.Label(self.root, text=self._admin_hint(), fg="#f9e2af",
                               bg="#1e1e2e", anchor="w")
        self.status.pack(fill="x", padx=12)

        self.text = scrolledtext.ScrolledText(
            self.root, bg="#11111b", fg="#cdd6f4", insertbackground="#cdd6f4",
            font=("Consolas", 10), relief="flat", wrap="none")
        self.text.pack(fill="both", expand=True, padx=12, pady=10)
        self.text.insert("1.0", "‘진단 시작’을 누르면 이 PC의 사양·상태·온도를 분석합니다.\n")
        self.text.config(state="disabled")

    def _admin_hint(self):
        if diagnostic.is_admin():
            return "관리자 권한: ✓ (CPU 온도까지 표시됩니다)"
        return "관리자 권한: ✗  — CPU 온도를 보려면 관리자로 실행하세요."

    def run(self):
        self.btn_run.config(state="disabled", text="분석 중...")
        self.btn_save.config(state="disabled")
        self._set_text("분석 중입니다... (몇 초 걸립니다)")
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        try:
            data = diagnostic.collect_all()
            report = diagnostic.report_text(data)
        except Exception as e:
            data, report = None, "진단 중 오류: %s" % e
        self.root.after(0, lambda: self._done(data, report))

    def _done(self, data, report):
        self.data = data
        self._set_text(report)
        self.btn_run.config(state="normal", text="🔍 다시 진단")
        self.btn_save.config(state=("normal" if data else "disabled"))
        self.btn_autofix.config(state=("normal" if data else "disabled"))
        if data:                     # 진단 결과를 이력에 자동 저장
            try:
                history.save(data, html=diagnostic.report_html(data))
            except Exception:
                pass

    def _set_text(self, s):
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", s)
        self.text.config(state="disabled")

    def save_html(self):
        if not self.data:
            return
        default = "PC진단리포트_%s.html" % (self.data.get("system", {}).get("hostname", "PC"))
        path = filedialog.asksaveasfilename(
            defaultextension=".html", initialfile=default,
            filetypes=[("HTML", "*.html")], title="리포트 저장")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(diagnostic.report_html(self.data))
            self.status.config(text="저장됨: %s" % path)
            try:
                os.startfile(path)      # 브라우저로 열기
            except Exception:
                pass
        except Exception as e:
            self.status.config(text="저장 실패: %s" % e)

    def repair_internet(self):
        admin = diagnostic.is_admin()
        msg = "인터넷 연결 문제를 복구합니다.\n\n• DNS 캐시 초기화\n• IP 주소 갱신"
        if admin:
            msg += "\n• Winsock 초기화 (재부팅 후 적용)\n• TCP/IP 초기화 (재부팅 후 적용)"
        else:
            msg += "\n\n(관리자로 실행하면 Winsock/TCP-IP 초기화까지 됩니다)"
        msg += "\n\n진행할까요?"
        if not messagebox.askyesno("인터넷 복구", msg):
            return
        self.btn_repair.config(state="disabled", text="복구 중...")
        threading.Thread(target=self._repair_worker, args=(admin,), daemon=True).start()

    def _repair_worker(self, admin):
        self.root.after(0, self._set_text, "[인터넷 복구] 진행 중...\n")

        def step(label, ok, status):
            mark = "✓" if ok else ("—" if ok is None else "✗")
            self.root.after(0, self._append, "  %s %s : %s" % (mark, label, status))

        res = repair.repair_internet(admin=admin, include_reset=admin, on_step=step)
        tail = "\n⚠️ 재부팅해야 적용됩니다." if res["reboot_needed"] else "\n완료."
        self.root.after(0, self._append, tail)
        self.root.after(0, lambda: self.btn_repair.config(state="normal", text="🌐 인터넷 복구"))

    def _append(self, s):
        self.text.config(state="normal")
        self.text.insert("end", "\n" + s)
        self.text.see("end")
        self.text.config(state="disabled")

    def win_repair(self):
        if not diagnostic.is_admin():
            messagebox.showwarning("Windows 복구",
                                   "관리자 권한으로 실행해야 합니다.\n프로그램을 우클릭 → 관리자 권한으로 실행")
            return
        if not messagebox.askyesno("Windows 복구",
                                    "시스템 파일 검사·복구(SFC /scannow)를 실행합니다.\n"
                                    "수 분 걸리며 중간에 닫지 마세요.\n\n진행할까요?"):
            return
        self.btn_winfix.config(state="disabled", text="복구 중...(수분)")
        threading.Thread(target=self._winfix_worker, daemon=True).start()

    def _winfix_worker(self):
        self.root.after(0, self._set_text,
                        "[Windows 복구] SFC 실행 중... 수 분 소요, 창을 닫지 마세요.\n")
        ok, msg = repair.run_sfc()
        self.root.after(0, self._append, (msg or "(출력 없음)")[-1500:])
        self.root.after(0, self._append, "\n" + ("완료" if ok else "오류/일부 실패"))
        self.root.after(0, lambda: self.btn_winfix.config(state="normal", text="🛠 Windows 복구"))

    def auto_repair(self):
        if not self.data:
            return
        a = analysis.analyze(self.data)
        admin = diagnostic.is_admin()
        actions = analysis.recommended_actions(a, admin=admin)
        if not actions:
            messagebox.showinfo("자동 복구",
                                "자동으로 실행할 복구 항목이 없습니다.\n"
                                "(관리자로 실행하면 시스템 파일 복구 등이 포함됩니다)")
            return
        names = {"flush_dns": "DNS 초기화", "renew_ip": "IP 갱신",
                 "reset_winsock": "Winsock 초기화", "reset_tcpip": "TCP/IP 초기화",
                 "run_sfc": "시스템 파일 복구(SFC)", "run_dism": "이미지 복구(DISM)"}
        lst = "\n".join("• " + names.get(k, k) for k in actions)
        if not messagebox.askyesno(
                "자동 복구",
                "분석 결과에 따라 다음을 실행합니다:\n\n%s\n\n"
                "(일부는 수 분 소요 / 재부팅 필요)\n진행할까요?" % lst):
            return
        self.btn_autofix.config(state="disabled", text="복구 중...")
        threading.Thread(target=self._autofix_worker, args=(actions,), daemon=True).start()

    def _autofix_worker(self, actions):
        self.root.after(0, self._set_text, "[자동 복구] 진행 중...\n")
        fnmap = {"flush_dns": repair.flush_dns, "renew_ip": repair.renew_ip,
                 "reset_winsock": repair.reset_winsock, "reset_tcpip": repair.reset_tcpip,
                 "run_sfc": repair.run_sfc, "run_dism": repair.run_dism}
        for k in actions:
            fn = fnmap.get(k)
            if not fn:
                continue
            self.root.after(0, self._append, "  → %s 실행..." % k)
            ok, _ = fn()
            self.root.after(0, self._append, "    %s" % ("완료" if ok else "실패"))
        self.root.after(0, self._append, "\n자동 복구 완료.")
        self.root.after(0, lambda: self.btn_autofix.config(state="normal", text="⚡ 자동 복구"))

    def show_history(self):
        self._set_text(history.format_list(history.list_history()))

    def show_installed(self):
        threading.Thread(target=self._installed_worker, daemon=True).start()

    def _installed_worker(self):
        self.root.after(0, self._set_text, "[설치 프로그램] 불러오는 중...")
        items = diagnostic.collect_installed()
        lines = ["[설치 프로그램] 총 %d개\n" % len(items)]
        for it in items:
            v = (" %s" % it["version"]) if it["version"] else ""
            lines.append("  %s%s" % (it["name"], v))
        self.root.after(0, self._set_text, "\n".join(lines))

    def start(self):
        self.root.mainloop()


if __name__ == "__main__":
    DiagnosticApp().start()
