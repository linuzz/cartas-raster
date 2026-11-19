# Cartas raster da DHN para o OpenCPN

Este repositório mantém as cartas náuticas **raster** (BSB/KAP) publicadas pela
[Diretoria de Hidrografia e Navegação](https://www.marinha.mil.br/chm) (DHN, Marinha do Brasil)
num catálogo que o plugin **Chart Downloader** (`chartdldr_pi`) do OpenCPN consegue ler.

O pipeline, toda semana:

1. lê o índice oficial no GeoNetwork do IDEM-DHN;
2. confere o ZIP de cada carta em `assets.marinha.mil.br`;
3. compara edição, tamanho e SHA-256 com o `data/manifest.json`;
4. publica o que mudou — direto, no modo `auto`, ou um pull request por carta, no modo `review`;
5. regenera o catálogo `RncProductCatalog` servido pelo GitHub Pages.

Os binários das cartas **não entram no Git** (limite de 100 MB por arquivo). Cada carta vira um ZIP na Release `cartas` (limite de 2 GB por asset).

## Direitos autorais

As cartas são da DHN. A DHN informa que as cartas raster gratuitas **não podem ser reproduzidas, compiladas ou derivadas para fins comerciais**, e que o uso comercial depende de autorização, tratada pela EMGEPRON (`chm.cartografia@marinha.mil.br`). Os arquivos GeoTIFF oficiais são só para uso acadêmico e **não** entram neste catálogo.

Este projeto só republica, para uso não comercial no OpenCPN, o ZIP raster que a própria DHN oferece para download. A carta raster não dispensa a carta de papel atualizada até o último Aviso aos Navegantes.

O código deste repositório (não as cartas) está sob a licença MIT. Veja [LICENSE](LICENSE).

## O que você precisa configurar no GitHub

O código já pede as permissões nos workflows. Estas opções do repositório precisam ser ligadas à mão, senão o Actions não consegue gravar o manifesto, abrir PR ou publicar o Pages:

1. **Settings → Actions → General → Workflow permissions**
   Escolha **Read and write permissions** e salve.
   Sem isso o `GITHUB_TOKEN` não faz commit nem envia a Release.

2. **Settings → Actions → General**
   Marque **Allow GitHub Actions to create and approve pull requests**.
   Sem isso o modo `review` não abre os PRs.

3. **Settings → Pages → Build and deployment → Source**
   Escolha **GitHub Actions** e salve.
   O catálogo passa a existir em
   `https://linuzz.github.io/cartas-raster/BR_RNCProdCat.xml`
   depois da primeira execução que publica o Pages (modo `auto`, ou o merge de um PR no modo `review`).

4. **Settings → Secrets and variables → Actions → Variables**
   Crie a variável de repositório **`UPDATE_MODE`** com o valor `auto` ou `review`.
   Se a variável não existir, o agendamento semanal usa `review`.

5. Se `main` tiver **branch protection** com revisão obrigatória, o GitHub Actions precisa de permissão para enviar commits em `main` (bypass da proteção para o Actions, ou um fluxo em que só o modo `review` é usado). No modo `auto` o workflow grava o manifesto direto em `main`.

Opcional, na mesma tela de variáveis:

| Variável | Padrão | Efeito |
| --- | --- | --- |
| `UPDATE_MODE` | `review` | `auto` publica sozinho. `review` abre um PR por carta. |
| `MAX_REVIEW_PRS` | `30` | Quantos PRs novos o modo `review` abre por execução. O resto fica para a semana seguinte. |
| `MIN_RESOLVED_RATIO` | `0.70` | Se menos do que esta fração das cartas tiver ZIP, o workflow **falha e não publica**. Serve para não esvaziar o catálogo quando o site da DHN muda de formato. |

Para rodar na hora: **Actions → Atualizar cartas raster → Run workflow**. O campo do modo, se preenchido, vale só para aquela execução e ignora a variável. A opção de simulação baixa e compara, mas não envia Release nem abre PR.

## Modos

### `auto`

Mudança de conteúdo vira upload na Release `cartas` e atualização do `data/manifest.json` e do catálogo. Não há passo humano.

Uma edição que já foi rejeitada no modo `review` (está em `data/rejected.json`) continua de fora até o arquivo na DHN mudar, mesmo depois de voltar para `auto`. Apague a entrada correspondente em `rejected.json` se quiser publicar essa edição mesmo assim.

O manifesto é gravado em `main` a cada 20 cartas enviadas. Se a execução cair no meio, o que já foi para a Release permanece. A execução seguinte não envia de novo um asset cujo SHA-256 na Release (`digest`) já é o do manifesto. O mesmo tamanho com outro conteúdo é enviado outra vez. Se o digest divergir, o workflow baixa de novo o ZIP da DHN, confere o SHA-256 do manifesto e republica.

### `review`

Cada carta nova ou com arquivo diferente abre **um pull request**. O PR não altera o manifesto compartilhado (dois PRs não conflitam): ele adiciona `proposals/<número>.json`. A descrição traz número, título, edição anterior e nova, tamanho, URL de origem na DHN e o trecho `<chart>` que entrará no catálogo.

- **Mesclar** o PR publica aquela carta. O workflow baixa o ZIP de novo, confere o SHA-256 aprovado, envia o asset e só então atualiza o manifesto e o catálogo.
- **Fechar** o PR sem mesclar rejeita aquela edição. Ela não é proposta outra vez até aparecer um arquivo novo.
- Um PR marcado com `superseded` foi trocado por uma edição mais nova e **não** conta como rejeição.

Enquanto já existe um PR aberto para a mesma carta e o mesmo SHA-256, outro não é aberto.

## Usar no OpenCPN

1. Instale o OpenCPN com o plugin Chart Downloader (vem com o programa).
2. Abra **Options** (Opções) → **Charts** (Cartas) → aba **Chart Downloader**.
3. Clique em **Add**.
4. Nome: `Cartas Raster DHN (Brasil)`.
5. URL do catálogo:

   `https://linuzz.github.io/cartas-raster/BR_RNCProdCat.xml`

6. Confirme, selecione o catálogo e clique em **Update** para baixar o XML.
7. Marque as cartas e clique em **Download**.
8. Em **Charts** → pastas de cartas, inclua a pasta onde o plugin gravou os ZIP descompactados e reconstrua o banco de cartas.

O catálogo é o formato NOAA `RncProductCatalog` que o plugin já entende (`number`, `title`, `zipfile_location`, `zipfile_datetime_iso8601`, `zipfile_size`, edições). Não é um esquema inventado. A URL dentro de `zipfile_location` aponta para o asset da Release deste repositório.

## De onde vêm as cartas

Conferido no site oficial:

- **Índice:** `https://idem.marinha.mil.br/geonetwork/` — busca JSON `srv/por/q` por “Carta Nautica Raster”. Em novembro de 2026 o índice anunciava 530 conjuntos, com formato `KAP/BSB` e link “Download da Carta Náutica - Raster”.
- **Arquivo:** `https://assets.marinha.mil.br/chm/sites/www.marinha.mil.br.chm/files/raster/<arquivo>.zip` (o endereço em `www.marinha.mil.br` redireciona para esse host). O ZIP já contém `.BSB` e `.KAP`. A edição usada no catálogo é a do cabeçalho BSB (`CED` / `NTM`), não só a data do metadado.
- **Nome do arquivo:** a DHN pode deixar várias gerações no ar ao mesmo tempo: `<número>.zip`, `<número>_0.zip`, `<número>_1.zip`, e por aí vai. O pipeline consulta todas com `HEAD` (para no primeiro sufixo ausente, mas sempre olha `.zip` e `_0`) e fica com a edição mais nova do cabeçalho BSB (`CED/SE`, depois a data `ED`, depois o ano do Aviso `NTM` e só então o número). `12.2019` e `2019-12` são o mesmo aviso; `3.2020` é mais novo que `12.2019`. A data do arquivo só desempata. Uma edição mais antiga nunca substitui a que já está publicada. Metadado e SHA das variantes já conferidas ficam em `data/variant_cache.json`, para não baixar de novo um ZIP cujo `HEAD` não mudou.
- **Resposta instável:** HTTP 403, 429 ou 5xx, e falha de rede, são erro temporário. O cliente espera (`Retry-After`, no máximo 60 s) e tenta de novo. Se continuar falhando, a carta entra em `data/unresolved.json` como `erro temporário` e a versão já publicada permanece. Isso não é tratado como “arquivo inexistente”.
- **Catálogo em papel:** o PDF da DHN só compara anos. `pdftotext` tem 120 s de limite. Se o PDF falhar ou estourar o tempo, a comparação é pulada e nenhuma carta é removida.
- **Avisos que não removem carta:** carta publicada que sumiu do índice da DHN (possível cancelamento) e número `CHT/NU` do BSB diferente do identificador. Os dois aparecem no resumo do job; o catálogo não muda por causa deles.
- **Retirada de carta duplicada:** o XML do catálogo é regravado antes de apagar o asset. No GitHub Actions a exclusão só roda depois que o Pages publicou esse XML.
- **Agendamento:** cada execução grava `data/last_checked.txt`. Esse commit mantém o workflow semanal ativo; o GitHub desliga agendamentos depois de 60 dias sem atividade no repositório.
- **Páginas HTML do CHM** (`www.marinha.mil.br/chm/dados-do-segnav-cartas-raster/...`) ficam atrás de um desafio do Cloudflare a partir de redes de datacenter. O workflow não depende delas: confirma o ZIP com `HEAD`. Se a página estiver acessível, o link do arquivo tem prioridade.
- O certificado de `*.marinha.mil.br` não envia o intermediário (AC Defesa / GlobalSign). O cliente TLS inclui esse intermediário e continua verificando a cadeia até a raiz pública. O certificado intermediário empacotado vale até agosto de 2029.

Se o JSON do índice mudar, a paginação repetir, o total cair abaixo de 400 cartas ou a maior parte dos ZIPs sumir, o comando termina com erro e **não publica**. Carta sem ZIP entra em `data/unresolved.json` e o restante segue, desde que a fração encontrada fique acima de `MIN_RESOLVED_RATIO`.

Para forçar o nome do ZIP de uma carta sem alterar código, edite `data/source_overrides.json`:

```json
{
  "version": 1,
  "zip_names": {
    "4342A": "4342A.zip"
  }
}
```

A chave é o identificador da carta (`3332`, `1831PL`, `4342A`, `4150-HS-C6`).

## Desenvolvimento

Requer Python 3.11+.

```bash
python -m pip install -e ".[dev]"
python -m pytest
python -m cartas_raster.cli check-catalog docs/BR_RNCProdCat.xml
```

O teste do catálogo reproduz as regras de `chartcatalog.cpp` do OpenCPN: raiz `RncProductCatalog`, elemento `<chart>`, datas que `wxDateTime::ParseDate` / `ParseFormat("%Y%m%d_%H%M%S")` aceitam, e `zipfile_datetime_iso8601` separado por `T` e `Z`, como faz o `wxStringTokenizer` do plugin.

Simular uma atualização sem publicar (ainda assim consulta a DHN):

```bash
python -m cartas_raster.cli run --mode review --dry-run
```
