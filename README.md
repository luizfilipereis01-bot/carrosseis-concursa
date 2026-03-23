# Criador de Carrosséis — Concursa AI

Sistema interno para a equipe de social media gerar e aplicar carrosséis virais
automaticamente no Canva usando o agente **Criador de Carrosséis** da Concursa AI.

---

## Como funciona

1. A social media digita um **comando** (ex: `carrossel sobre concurso da Receita Federal 2025`)
2. Cola o **link de edição do Canva** de um design template
3. Clica em **Gerar Carrossel**
4. O sistema:
   - Chama o Claude (agente Criador de Carrosséis) para gerar o roteiro completo
   - Aplica o conteúdo gerado diretamente no design do Canva via API
   - Retorna o **mesmo link Canva**, agora com o conteúdo preenchido

---

## Comandos disponíveis

| Comando | Descrição |
|---|---|
| `carrossel sobre [tema]` | Gera carrossel direto sobre o tema especificado |
| `varredura` | Pesquisa temas em alta e apresenta opções sem gerar ainda |
| `versão TikTok` | Adapta o carrossel para linguagem TikTok |
| `versão Instagram` | Versão mais visual e emocional |
| `legenda completa` | Inclui legenda otimizada para o post |
| `3 opções de capa` | Gera 3 variações do Slide 1 para teste A/B |
| `refaz mais viral` | Reescreve o carrossel com gatilhos mais agressivos |

---

## Instalação

### Pré-requisitos
- Python 3.11+
- Chave da API Anthropic
- Credenciais da Canva Connect API (ver seção abaixo)

### Setup

```bash
# 1. Clone o repositório
cd /workspaces/carrosseis-concursa

# 2. Crie o arquivo de variáveis de ambiente
cp .env.example .env
# Preencha ANTHROPIC_API_KEY e CANVA_ACCESS_TOKEN no .env

# 3. Instale as dependências
cd backend
pip install -r requirements.txt

# 4. Inicie o servidor
uvicorn main:app --reload --port 8000
```

Acesse: **http://localhost:8000**

---

## Configuração do Canva

### Opção A — Editing Session API (recomendado)
Funciona com qualquer design que o usuário autenticado possui.

O sistema lê os elementos de texto de cada slide (ordenados de cima para baixo)
e substitui:
- **1º elemento de texto** → headline do slide
- **2º elemento de texto** → corpo do slide

### Opção B — Autofill API (requer Canva Enterprise)
Funciona com **brand templates** que possuem campos nomeados.

Convenção de nomes dos campos no template Canva:
```
slide_1_headline   → Headline do slide 1
slide_1_body       → Corpo do slide 1
slide_2_headline   → Headline do slide 2
slide_2_body       → Corpo do slide 2
...até slide_12
```

### Obtendo o Access Token

1. Crie um app em https://www.canva.com/developers/
2. Configure os escopos: `design:content:read design:content:write`
3. Defina Redirect URI: `http://localhost:8000/auth/callback`
4. Use o fluxo OAuth 2.0 com PKCE para obter o token
5. Cole o `access_token` no `.env` como `CANVA_ACCESS_TOKEN`

> O token expira em ~4 horas. Para produção, implemente o refresh automático.

---

## Estrutura do projeto

```
carrosseis-concursa/
├── backend/
│   ├── main.py                   # API FastAPI
│   ├── services/
│   │   ├── claude_service.py     # Integração Claude (Criador de Carrosséis)
│   │   └── canva_service.py      # Integração Canva API
│   └── requirements.txt
├── frontend/
│   └── index.html                # Interface web
├── .env.example
└── README.md
```

---

## API

### `POST /api/generate`

**Body:**
```json
{
  "command": "carrossel sobre concurso da Receita Federal 2025",
  "canva_link": "https://www.canva.com/design/DAF.../edit"
}
```

**Response:**
```json
{
  "success": true,
  "edit_url": "https://www.canva.com/design/DAF.../edit",
  "canva_applied": true,
  "slides_count": 9,
  "theme": "Concurso Receita Federal 2025",
  "platform": "Instagram",
  "post_time": "Terça a quinta, 18h–20h",
  "hashtags": ["#concursopublico", "#receitafederal", ...],
  "caption": "...",
  "slides": [
    { "number": 1, "type": "cover", "headline": "...", "body": "..." },
    ...
  ],
  "raw": "roteiro completo em texto"
}
```

Se `canva_applied` for `false`, o conteúdo foi gerado mas não aplicado automaticamente
ao Canva (use o `raw` ou os `slides` para preencher manualmente).
