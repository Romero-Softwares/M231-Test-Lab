from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from usb_machine_emulator.control import MachinePanelController, PanelSnapshot


FORCE_UNIT_TO_KN = {"N": 0.001, "kN": 1.0, "lbf": 0.0044482216152605, "kgf": 0.00980665}


def _force_from_kn(value_kn: float, unit: str) -> float:
    return value_kn / FORCE_UNIT_TO_KN[unit]


class MachinePanel(tk.Tk):
    """Painel/display da maquina virtual, separado do software M231Pro."""

    def __init__(self, controller: MachinePanelController, period_ms: int = 20) -> None:
        super().__init__()
        self.controller = controller
        self.period_ms = period_ms
        self.title("M231 — Painel da máquina virtual")
        self.geometry("620x480")
        self.minsize(560, 480)
        self.configure(background="#171b1d")
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._closing = False
        self._build()

    def _build(self) -> None:
        style = ttk.Style(self)
        style.configure("Panel.TFrame", background="#171b1d")
        style.configure("Panel.TLabel", background="#171b1d", foreground="#d9e1e5")
        body = ttk.Frame(self, style="Panel.TFrame", padding=18)
        body.pack(fill="both", expand=True)

        ttk.Label(
            body,
            text="M231  •  CONTROLE LOCAL DA MÁQUINA",
            style="Panel.TLabel",
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            body,
            text="Compatível somente com o M231Pro.",
            style="Panel.TLabel",
        ).pack(anchor="w", pady=(2, 12))

        display = tk.Frame(
            body,
            background="#06120c",
            highlightbackground="#4b6657",
            highlightthickness=2,
            padx=16,
            pady=13,
        )
        display.pack(fill="x")
        self.status_var = tk.StringVar(value="AGUARDANDO SOFTWARE")
        self.position_var = tk.StringVar(value="POSIÇÃO  ----- mm")
        self.force_var = tk.StringVar(value="CARGA    ----- kN")
        self.speed_var = tk.StringVar(value="VELOC.   ----- mm/min")
        self.frames_var = tk.StringVar(value="USB: telemetria pronta • painel bloqueado")
        self.source_var = tk.StringVar(value="COMANDO: AGUARDANDO SOFTWARE")
        self.status_label = tk.Label(
            display,
            textvariable=self.status_var,
            background="#06120c",
            foreground="#ffd166",
            font=("Consolas", 12, "bold"),
        )
        self.status_label.pack(anchor="e")
        for variable, font_size in (
            (self.position_var, 20),
            (self.force_var, 20),
            (self.speed_var, 13),
        ):
            tk.Label(
                display,
                textvariable=variable,
                background="#06120c",
                foreground="#caffd8",
                anchor="w",
                font=("Consolas", font_size, "bold"),
            ).pack(fill="x", pady=(4, 0))
        tk.Label(
            display,
            textvariable=self.source_var,
            background="#06120c",
            foreground="#8dbf9a",
            anchor="w",
            font=("Consolas", 10),
        ).pack(fill="x", pady=(9, 0))
        tk.Label(
            display,
            textvariable=self.frames_var,
            background="#06120c",
            foreground="#8dbf9a",
            anchor="w",
            font=("Consolas", 9),
        ).pack(fill="x", pady=(2, 0))

        controls = ttk.LabelFrame(body, text="Controle manual da máquina", padding=12)
        controls.pack(fill="x", pady=(14, 0))
        speed_row = ttk.Frame(controls)
        speed_row.pack()
        ttk.Label(speed_row, text="Velocidade").pack(side="left")
        self.manual_speed_var = tk.StringVar(value="100")
        self.speed_entry = ttk.Entry(speed_row, width=9, textvariable=self.manual_speed_var)
        self.speed_entry.pack(side="left", padx=6)
        ttk.Label(speed_row, text="mm/min").pack(side="left")

        buttons = ttk.Frame(controls)
        buttons.pack(pady=(10, 6))
        self.up_button = ttk.Button(buttons, text="▲  SUBIR", command=lambda: self._jog(1))
        self.stop_button = ttk.Button(buttons, text="■  PARAR", command=self._stop)
        self.down_button = ttk.Button(buttons, text="▼  DESCER", command=lambda: self._jog(-1))
        self.up_button.grid(row=0, column=0, padx=4)
        self.stop_button.grid(row=0, column=1, padx=4)
        self.down_button.grid(row=0, column=2, padx=4)

        zeros = ttk.Frame(controls)
        zeros.pack()
        self.zero_position_button = ttk.Button(
            zeros, text="Zerar posição", command=self._zero_position
        )
        self.zero_force_button = ttk.Button(
            zeros, text="Zerar carga", command=self._zero_force
        )
        self.zero_position_button.pack(side="left", padx=4)
        self.zero_force_button.pack(side="left", padx=4)
        self._set_controls(False)

    def _set_controls(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for widget in (
            self.speed_entry,
            self.up_button,
            self.stop_button,
            self.down_button,
            self.zero_position_button,
            self.zero_force_button,
        ):
            widget.configure(state=state)

    def _manual_speed(self) -> float:
        try:
            speed = float(self.manual_speed_var.get().replace(",", "."))
        except ValueError as error:
            raise ValueError("Informe uma velocidade manual válida.") from error
        if speed <= 0:
            raise ValueError("A velocidade manual deve ser maior que zero.")
        return speed

    def _run_control(self, action) -> None:
        try:
            action()
        except (RuntimeError, ValueError) as error:
            messagebox.showwarning("Painel M231", str(error), parent=self)

    def _jog(self, direction: int) -> None:
        self._run_control(lambda: self.controller.manual_jog(direction, self._manual_speed()))

    def _stop(self) -> None:
        self._run_control(self.controller.manual_stop)

    def _zero_position(self) -> None:
        self._run_control(self.controller.zero_position)

    def _zero_force(self) -> None:
        self._run_control(self.controller.zero_force)

    def update_snapshot(self, snapshot: PanelSnapshot) -> None:
        self.status_var.set(snapshot.status_text)
        self._set_controls(snapshot.synchronized)
        self.status_label.configure(
            foreground="#75f09b" if snapshot.synchronized else "#ffd166"
        )
        if snapshot.synchronized:
            self.position_var.set(f"POSIÇÃO  {snapshot.position_mm:+09.2f} mm".replace(".", ","))
            force = _force_from_kn(snapshot.force_kn, snapshot.force_unit)
            self.force_var.set(
                f"CARGA    {force:+09.3f} {snapshot.force_unit}".replace(".", ",")
            )
            self.speed_var.set(
                f"VELOC.   {snapshot.speed_mm_min:+09.1f} mm/min".replace(".", ",")
            )
            self.frames_var.set(
                f"USB: sincronizada • {snapshot.compatibility_text} • "
                f"{snapshot.frame_count:08d} quadros"
            )
        else:
            self.position_var.set("POSIÇÃO  ----- mm")
            self.force_var.set(f"CARGA    ----- {snapshot.force_unit}")
            self.speed_var.set("VELOC.   ----- mm/min")
            self.frames_var.set(
                f"USB: pronta para {snapshot.compatibility_text} • "
                f"fila {snapshot.queued_telemetry_bytes:03d} B • painel bloqueado"
            )
        self.source_var.set(f"COMANDO: {snapshot.command_source}")

    def start(self) -> None:
        try:
            self.controller.start()
        except OSError as error:
            messagebox.showerror(
                "Driver M231 indisponível",
                f"Não foi possível abrir o controlador USB virtual.\n\n{error}",
                parent=self,
            )
            self.status_var.set("DRIVER INDISPONÍVEL")
            return
        self.after(self.period_ms, self._poll)

    def _poll(self) -> None:
        if self._closing:
            return
        try:
            self.update_snapshot(self.controller.tick())
        except (OSError, RuntimeError) as error:
            self.status_var.set("COMUNICAÇÃO INTERROMPIDA")
            self.status_label.configure(foreground="#ff7474")
            self._set_controls(False)
            self.frames_var.set(str(error))
        else:
            self.after(self.period_ms, self._poll)

    def _close(self) -> None:
        self._closing = True
        try:
            self.controller.close()
        finally:
            self.destroy()
