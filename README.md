# M231 Test Lab

Programador independente para criar e validar sequências de ensaio de compressão,
manutenção de posição e descompressão/retorno.

## Regra do ensaio

O movimento é controlado e validado por **posição/comprimento** e por **velocidade de
deslocamento**. A carga não é um alvo de controle: ela é somente um limite de
segurança. Se a leitura ultrapassar o limite configurado, o ensaio é interrompido
para proteger a máquina e o componente.

No modelo padrão:

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

## Estrutura do codigo

```text
main.py                    entrada da aplicacao
m231pro/
  config.py                caminhos compartilhados
  core/simulation.py       regras, modelos e simulacao dos ensaios
  core/storage.py          historico SQLite de execucoes e aquisicoes
  hardware/backend.py      API unica da maquina e backend local
  hardware/ftdi.py         descoberta e transporte D2XX
  hardware/m231.py         protocolo e backend M231 nativos em Python
  ui/app.py                interface grafica e apresentacao dos resultados
tests/                     testes automatizados por responsabilidade
driver/                    somente a DLL D2XX necessaria para a FTDI
```

Os modulos Python da aplicacao ficam no pacote `m231pro`; somente o ponto de
entrada permanece na raiz.

O pacote Windows e independente do programa legado. Ele inclui somente
`driver/ftd2xx64.dll`; executaveis, bancos, relatorios e demais recursos do
AnyTest/TESTPro nao fazem parte da distribuicao.

O programa permite:

- continuar funcionando normalmente com a máquina offline, mantendo criação,
  edição, simulação, gráficos, arquivos e relatórios disponíveis em modo local;
- montar, editar e reordenar etapas;
- definir posição-alvo, velocidade, duração, espera antes da descompressão,
  intervalo de aquisição, limites de posição, carga de segurança e ciclos;
- simular compressão e descompressão antes de usar a máquina;
- salvar/abrir programas em JSON;
- comandar subida, descida, parada, retorno e zeragens pela mesma API em modo
  local ou físico;
- manter histórico de ensaios e amostras em `data/m231pro-results.sqlite3`;
- gerar pelo histórico um relatório PDF paginado com identificação, configuração,
  resumo, curva carga × deslocamento e tabela completa das aquisições;
- reexportar em CSV qualquer ensaio salvo no histórico, sem depender do resultado
  que está aberto na tela;
- visualizar o gráfico posição × tempo;
- exportar em CSV somente os pontos adquiridos no intervalo espacial configurado,
  além do início, dos extremos de percurso, do fim da espera e de uma eventual
  parada de segurança;
- registrar no CSV o intervalo e a espera configurados, a variação de posição,
  o tempo entre pontos e a velocidade calculada, permitindo conferir compressão
  e retorno sem interpretar como movimento o tempo parado;
- manter a carga em 0 kN na simulação e identificá-la no CSV como
  `carga_sem_sensor_kn`; o limite configurado não é usado para fabricar leituras;
- bloquear a exportação se a programação tiver sido alterada depois da simulação,
  evitando misturar um resultado antigo com os valores atuais da tela.

## Operação offline e integração M221C

Sem uma máquina conectada, o sistema permanece integralmente utilizável pelo backend
local: programação, controle manual simulado, execução, gráficos, importação de
métodos, CSV e histórico continuam ativos, e o indicador mostra **MÁQUINA OFFLINE**.

**Detectar hardware** faz somente enumeração D2XX. **Conectar máquina** abre a FTDI
diretamente no processo Python, configura os parâmetros da M231 e exige uma
telemetria válida antes de habilitar o controle. Nenhum processo PowerShell, assembly
ou `TESTPro.exe` é iniciado. Movimento, parada, zeragens, retorno, aquisição e os
intertravamentos de carga/curso usam o protocolo binário nativo da controladora.

O codec do protocolo, quadros de movimento, telemetria fragmentada, parada no alvo,
limite de carga e todos os fluxos offline são testáveis sem máquina.
A confirmação final de escalas, polaridade, fins de curso, emergência e resposta dos
sensores exige um ensaio assistido com o equipamento físico conectado.

## Testes automatizados

```powershell
python -m unittest discover -s tests -v
```
