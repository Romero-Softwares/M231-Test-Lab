# M231 Test Lab

Programador independente para criar e validar sequências de ensaio de compressão,
tração, manutenção de posição e descompressão/retorno.

## Ensaios disponíveis

O movimento é controlado e validado pela posição/comprimento e pela velocidade de
deslocamento. A carga não é um alvo de controle na compressão: ela funciona como
limite de segurança. Se a leitura ultrapassar o limite configurado, o ensaio é
interrompido para proteger a máquina e o componente.

O teste de tração possui um fluxo próprio, com controle por carga-alvo, duração,
velocidade e intervalo de aquisição. O sistema oferece modelos para:

- tração de pino, com referência ASTM F606/F606M;
- tração de mola, com procedimento configurável;
- retorno automático à posição inicial, quando habilitado;
- parada por limite de carga, perda de telemetria ou condição de segurança.

No modelo padrão de compressão e retorno:

- velocidade de compressão: 100 mm/min;
- limite de carga de segurança: 61 kN;
- compressão até a posição configurada;
- espera configurável antes de iniciar a descompressão;
- descompressão automática até a posição inicial, na mesma velocidade usada na
  compressão;
- aquisição por deslocamento configurável (por padrão, um ponto a cada 10 mm),
  sem reduzir a frequência interna de verificação da carga de segurança;
- limites mínimo e máximo de posição validados antes da execução.

## Executar

```powershell
python main.py
```

## Estrutura do código

```text
main.py                    entrada da aplicação
m231pro/
  config.py                caminhos compartilhados
  core/simulation.py       regras, modelos e simulação dos ensaios
  core/storage.py          histórico SQLite de execuções e aquisições
  hardware/backend.py      API única da máquina e backend local
  hardware/ftdi.py         descoberta e transporte D2XX
  hardware/m231.py         protocolo e backend M231 nativos em Python
  ui/app.py                interface gráfica e apresentação dos resultados
tests/                     testes automatizados por responsabilidade
driver/                    DLLs D2XX necessárias para a FTDI
```

Os módulos Python da aplicação ficam no pacote `m231pro`; somente o ponto de
entrada permanece na raiz.

## Recursos

- funcionamento offline para criação, edição, simulação, gráficos, arquivos e
  relatórios;
- montagem, edição e reordenação de etapas;
- configuração de posição-alvo, velocidade, duração, espera, intervalo de
  aquisição, limites de posição, carga de segurança, carga-alvo e ciclos;
- simulação de compressão, tração e retorno antes do uso da máquina;
- salvamento e abertura de programas em JSON;
- comandos de subida, descida, tração, parada, retorno e zeragem pela mesma API
  local ou física;
- histórico de ensaios e amostras em `data/m231pro-results.sqlite3`;
- relatório PDF paginado com identificação, configuração, resumo, curva e tabela
  completa das aquisições;
- exportação em CSV de qualquer ensaio salvo no histórico;
- gráfico de posição × tempo e, nos ensaios de tração, carga × tempo;
- registro no CSV do intervalo, da espera, da variação de posição, do tempo entre
  pontos e da velocidade calculada;
- bloqueio da exportação quando a programação foi alterada depois da simulação,
  evitando misturar um resultado antigo com os valores atuais da tela.

## Operação offline e integração M231C

Sem uma máquina conectada, o sistema permanece integralmente utilizável pelo
backend local. Programação, controle manual simulado, execução, gráficos,
importação de métodos, CSV e histórico continuam ativos, e o indicador mostra
**MÁQUINA OFFLINE**.

**Detectar hardware** faz somente enumeração D2XX. **Conectar máquina** abre a
FTDI diretamente no processo Python, configura os parâmetros da M231 e exige uma
telemetria válida antes de habilitar o controle. Nenhum processo PowerShell,
assembly ou `TESTPro.exe` é iniciado. Movimento, parada, zeragem, retorno,
aquisição e intertravamentos usam o protocolo binário nativo da controladora.

O codec do protocolo, os quadros de movimento, a telemetria fragmentada, a parada
no alvo, o limite de carga e os fluxos offline são testáveis sem a máquina. A
confirmação final de escalas, polaridade, fins de curso, emergência e resposta dos
sensores exige um ensaio assistido com o equipamento físico conectado.

## Releases Windows

O pacote Windows inclui as DLLs D2XX física e virtual nas arquiteturas x86 (32
bits) e x64 (64 bits). O pacote x86 deve ser gerado com Python/PyInstaller x86;
adicionar uma DLL x86 a um executável x64 não transforma o aplicativo em 32 bits.

O script identifica a arquitetura do interpretador usado e produz:

- `release/M231Pro-Primeira-Versao-Windows-x32.zip` com Python x86;
- `release/M231Pro-Primeira-Versao-Windows-x64.zip` com Python x64.

As releases atualmente versionadas foram geradas e verificadas nas duas
arquiteturas:

- x86/32-bit: `M231Pro-Primeira-Versao-Windows-x32.zip`, gerada com Python
  32-bit e confirmada como executável PE x86;
- x64/64-bit: `M231Pro-Primeira-Versao-Windows-x64.zip`, gerada com Python
  64-bit e confirmada como executável PE x64.

Os hashes SHA-256 publicados para conferência são `D617B2D4ECE90BD55A7BDF419D4328FAA5D498C56EE4D2FB96DACF6ADE0B2281`
para x86 e `5D61933F3AD888B56492ADA74AAB8007E8296B55122A648B4F988347FFBD9B3D`
para x64.

Para gerar uma arquitetura específica, execute o script com o interpretador
correspondente:

```powershell
powershell -ExecutionPolicy Bypass -File .\build_release.ps1 -Python C:\caminho\python-x86\python.exe
powershell -ExecutionPolicy Bypass -File .\build_release.ps1 -Python C:\caminho\python-x64\python.exe
```

O build valida a presença das quatro DLLs, remove arquivos do programa legado e
exibe o SHA-256 do ZIP gerado.

## Testes automatizados

```powershell
python -m unittest discover -s tests -v
```

Os testes cobrem a simulação de compressão e tração, a aquisição temporal, o
retorno, a persistência, os relatórios, o protocolo M231, o backend FTDI e o
emulador USB. O teste físico final deve ser feito sem carga e com procedimento de
segurança definido para o equipamento conectado.
