# Emulador USB fisico da M231

Este subprojeto implementa o requisito de o Windows enumerar uma maquina no
barramento USB. Nao e uma porta TCP e nao e apenas uma simulacao interna do
M231Pro.

## Arquitetura

1. `M231VirtualUsb.sys` e um driver KMDF/UdeCx que cria um controlador USB 2.0
   virtual.
2. Quando o host envia `IOCTL_M231_PLUG_IN`, o driver conecta na porta virtual
   um dispositivo FT232R compativel (`VID 0403`, `PID 6001`, bulk IN `0x81`,
   bulk OUT `0x02`).
3. O driver FTDI do Windows liga-se ao dispositivo enumerado. A
   `ftd2xx64.dll` do M231Pro (64 bits) passa a enxergar o dispositivo pela
   lista D2XX.
4. `python -m usb_machine_emulator.run` abre o painel/display da maquina,
   produz os quadros binarios `0x7f` e recebe os comandos de velocidade
   `0`/`X` atraves do driver.
5. Ao fechar o emulador, `IOCTL_M231_PLUG_OUT` simula a remocao fisica da USB.

## Painel e sincronizacao

O controle manual pertence a este subprojeto, nao a interface principal do
M231Pro. Ao iniciar, o emulador envia a telemetria necessaria para o software
encontrar a controladora, mas o display oculta os valores e todos os botoes
ficam bloqueados. O driver conta as leituras USB realmente realizadas pelo
software principal. Somente depois que essa leitura comeca o painel mostra
posicao, carga e velocidade e libera subir, descer, parar e os zeros.

Se o software conectado deixar de ler ou escrever na USB por mais de tres segundos, o
emulador zera a velocidade por seguranca, esconde os valores e bloqueia o
painel novamente. O contrato v3 do driver contabiliza tanto leituras quanto
escritas do host; drivers antigos continuam decodificaveis, mas devem ser
atualizados para que os dois softwares tenham a mesma deteccao de atividade.

Por padrao, o emulador atende somente ao M231Pro pelo canal compartilhado. O modo
`--compatibility-mode` permite validar o transporte compatível. Como a D2XX abre
uma FT232R com acesso exclusivo, mantenha uma única sessão por vez.

A fila de entrada mantem no maximo um quadro de telemetria aguardando consumo.
Assim, abrir qualquer um dos programas depois de varios minutos nao entrega um
historico atrasado nem comeca a sessao com valores obsoletos.

O driver e instalado uma unica vez, com privilegio de Administrador. Cada
execucao do emulador conecta e remove o conversor virtual; reinstalar um driver
de kernel em toda abertura seria inseguro e desnecessario.

## Estado da primeira entrega

- Contrato de IOCTL compartilhado entre driver e host pronto.
- Descritores USB FT232R e endpoints bulk implementados.
- Comandos de controle FTDI basicos, fila RX/TX com contrapressao e conexao/remocao UdeCx
  implementados.
- Modelo de movimento e telemetria M231 ligado ao mesmo codec usado pelo
  software principal.
- Testes de compatibilidade de quadros e comandos incluidos na suite do projeto.

Ainda e obrigatorio validar o driver em uma maquina de teste com o WDK e modo
de assinatura de teste. Driver de kernel x64 nao pode ser carregado sem uma
assinatura aceita pelo Windows. Para distribuicao normal, o `.sys`/catalogo
precisa de assinatura Microsoft/Attestation; a assinatura local serve apenas
para desenvolvimento.

## Compilar

Requisitos: Windows 10/11 x64, Visual Studio 2022 com C++, componente Windows
Driver Kit 26100 e as bibliotecas Spectre x64 instalados pelo Visual Studio
Installer.

```powershell
powershell -ExecutionPolicy Bypass -File usb_machine_emulator\build_driver.ps1
```

## Instalar no PC de teste

Abra PowerShell como Administrador e execute:

```powershell
powershell -ExecutionPolicy Bypass -File usb_machine_emulator\install_driver.ps1
```

Depois, sem privilegio administrativo:

```powershell
powershell -ExecutionPolicy Bypass -File usb_machine_emulator\run_emulator.ps1
```

Para automacao sem janela, use:

```powershell
python -m usb_machine_emulator.run --headless
```

Para testar o modo de compatibilidade (somente em uma maquina de teste):

```powershell
python -m usb_machine_emulator.run --compatibility-mode
```

Se o teste der errado, encerre o emulador; o modo padrao continua sem o canal
fisico e nenhuma alteracao permanente no sistema e feita.

Ao iniciar, o Gerenciador de Dispositivos deve mostrar o controlador virtual e
o conversor FTDI. O teste de aceite final e `FT_CreateDeviceInfoList` listar
`A501GR6Y` e o M231Pro receber telemetria valida.

## Identidade USB

`0403:6001` e o identificador padrao documentado pela FTDI para FT232R e e o
par que a D2XX Windows reconhece sem alterar o INF assinado. Antes de distribuir
o produto fora de um laboratorio, a identidade USB e a permissao de uso devem
ser formalizadas com a FTDI, ou deve ser adquirido um VID/PID proprio e um
pacote de driver devidamente assinado.
