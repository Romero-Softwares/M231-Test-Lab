from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time

# Permite tanto ``python -m usb_machine_emulator.run`` quanto a execucao
# direta ``python usb_machine_emulator/run.py`` ou ``python run.py``.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from usb_machine_emulator.control import MachinePanelController
from usb_machine_emulator.panel import MachinePanel
from usb_machine_emulator.shared_transport import PhysicalAndSharedTransport


def run(period_s: float, compatibility_text: str) -> None:
    controller = MachinePanelController(
        PhysicalAndSharedTransport(),
        compatibility_text=compatibility_text,
    )
    controller.start()
    print(
        "M231 virtual pronta para o M231Pro. "
        "Pressione Ctrl+C para remover."
    )
    try:
        while True:
            controller.tick()
            time.sleep(period_s)
    finally:
        controller.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Emulador USB fisico da M231")
    parser.add_argument("--period-ms", type=float, default=20.0)
    parser.add_argument(
        "--headless",
        action="store_true",
        help="executa sem o display e o controle manual da maquina",
    )
    parser.add_argument(
        "--compatibility-mode",
        action="store_true",
        help="identifica o canal virtual como compatibilidade",
    )
    args = parser.parse_args()
    if not 2.0 <= args.period_ms <= 1000.0:
        parser.error("--period-ms deve ficar entre 2 e 1000")
    compatibility_text = "Modo de compatibilidade" if args.compatibility_mode else "M231Pro"
    if args.headless:
        run(args.period_ms / 1000.0, compatibility_text)
        return
    controller = MachinePanelController(
        PhysicalAndSharedTransport(),
        compatibility_text=compatibility_text,
    )
    panel = MachinePanel(controller, period_ms=round(args.period_ms))
    panel.start()
    panel.mainloop()


if __name__ == "__main__":
    main()
