"""
Backend Flask - Extrator de Notas Colégio Ativo
Recebe PDFs via POST, extrai dados com pdfplumber (100% preciso)
e retorna CSV pronto para importar no SIGA.
Detecta disciplinas automaticamente pelo cabeçalho da tabela do PDF.
"""

from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import pdfplumber
import io
import csv
import re
import unicodedata

app = Flask(__name__)
CORS(app)

# ============================================================
# CONFIGURAÇÕES
# ============================================================

# Palavras que indicam que a coluna NÃO é uma disciplina
COLUNAS_IGNORAR = {
    "N", "Nº", "NUMERO", "ALUNO", "ALUNO(A)", "NOME", "SITUACAO",
    "SITUAÇÃO", "SITUACAO FINAL", "SITUAÇÃO FINAL", "FREQUENCIA",
    "FREQUÊNCIA", "FREQUENCIA ANUAL", "FREQUÊNCIA ANUAL",
    "CARGA HORARIA", "CARGA HORÁRIA", "TOTAL DE FALTAS", "F", ""
}

# ============================================================
# FUNÇÕES AUXILIARES
# ============================================================

def normalizar(texto):
    """Remove acentos e converte para maiúsculas para comparação."""
    if not texto:
        return ""
    nfkd = unicodedata.normalize('NFKD', str(texto))
    return "".join(c for c in nfkd if not unicodedata.combining(c)).upper().strip()


def limpar_nota(valor):
    """Converte nota brasileira (7,5) para string com vírgula ou retorna None."""
    if valor is None:
        return None
    v = str(valor).strip()
    if v in ("---", "", "0,0", "0.0"):
        return None
    v = v.replace(",", ".").strip()
    try:
        f = float(v)
        if 0.1 <= f <= 10.0:
            return str(f).replace(".", ",")
        return None
    except ValueError:
        return None


def detectar_disciplinas(cabecalho_pdf):
    """
    A partir do cabeçalho da tabela do PDF, detecta quais colunas
    são disciplinas e retorna lista de (nome_disciplina, indice_coluna).
    A tabela tem pares [DISCIPLINA | FALTAS] — detecta apenas as disciplinas.
    """
    disciplinas = []
    i = 0
    while i < len(cabecalho_pdf):
        cel = str(cabecalho_pdf[i] or "").strip()
        cel_norm = normalizar(cel)

        # Ignora colunas conhecidas que não são disciplinas
        if cel_norm in {normalizar(x) for x in COLUNAS_IGNORAR}:
            i += 1
            continue

        # Se a célula tem texto e não está na lista de ignorar,
        # assume que é uma disciplina — a próxima coluna é faltas (pula)
        if cel and len(cel) > 1:
            # Limpa o nome: remove quebras de linha e espaços extras
            nome = " ".join(cel.split())
            # Cabeçalhos verticais no PDF são lidos invertidos pelo pdfplumber
            # Reverte o nome se o texto invertido tiver mais vogais (mais legível)
            nome_rev = nome[::-1]
            vogais = set("AEIOUaeiouAEIOUaeiou")
            def score(s):
                return sum(1 for c in s if c in vogais) / max(len(s), 1)
            if score(nome_rev) > score(nome):
                nome = nome_rev
            disciplinas.append((nome, i))
            i += 2  # pula a coluna de faltas
        else:
            i += 1

    return disciplinas


# ============================================================
# FUNÇÕES DE EXTRAÇÃO
# ============================================================

def extrair_notas_pdf(pdf_bytes):
    """
    Extrai alunos, disciplinas e notas do PDF de ata.
    Detecta as disciplinas automaticamente pelo cabeçalho da tabela.
    Retorna: (lista_alunos, lista_disciplinas)
    """
    alunos = []
    disciplinas_detectadas = []

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            tabela = page.extract_table()
            if not tabela:
                continue

            # Encontra o cabeçalho da tabela (linha com nomes de disciplinas)
            cabecalho_idx = None
            for idx, row in enumerate(tabela):
                if not row:
                    continue
                # O cabeçalho tem células como "PORTUGUÊS", "MATEMÁTICA" etc
                # Identifica pela presença de texto longo em múltiplas células
                celulas_texto = [c for c in row if c and len(str(c).strip()) > 2]
                if len(celulas_texto) >= 5 and not str(row[0] or "").strip().isdigit():
                    # Verifica se parece um cabeçalho de disciplinas
                    # (não começa com número como linhas de alunos)
                    cabecalho_idx = idx
                    break

            if cabecalho_idx is not None and not disciplinas_detectadas:
                # Junta linhas do cabeçalho (pode ser multi-linha no PDF)
                cab = []
                for idx in range(cabecalho_idx, min(cabecalho_idx + 3, len(tabela))):
                    row = tabela[idx]
                    if str(row[0] or "").strip().isdigit():
                        break
                    for j, cel in enumerate(row):
                        if j >= len(cab):
                            cab.append(str(cel or "").strip())
                        elif cel and str(cel).strip():
                            cab[j] = (cab[j] + " " + str(cel).strip()).strip()

                disciplinas_detectadas = detectar_disciplinas(cab)

            # Extrai linhas de alunos
            for row in tabela:
                if not row or not row[0]:
                    continue
                num = str(row[0]).strip()
                if not num.isdigit():
                    continue

                nome = str(row[1]).strip() if len(row) > 1 and row[1] else ""
                if not nome or len(nome) < 3:
                    continue

                situacao = str(row[-1]).strip() if row[-1] else ""

                # Extrai notas usando os índices detectados
                notas = []
                for nome_disc, col_idx in disciplinas_detectadas:
                    if col_idx < len(row):
                        notas.append(limpar_nota(row[col_idx]))
                    else:
                        notas.append(None)

                alunos.append({
                    "nome": nome.upper(),
                    "situacao": situacao,
                    "notas": notas,
                })

    nomes_disciplinas = [d[0] for d in disciplinas_detectadas]
    return alunos, nomes_disciplinas


def extrair_cadastro_pdf(pdf_bytes):
    """
    Extrai dados cadastrais do PDF de relação de alunos (SIGA).
    Formato: Nº | Matrícula | Nome
             Filiação 2 / CPF / Data de nascimento
    """
    alunos = []

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        texto_completo = ""
        for page in pdf.pages:
            texto_completo += (page.extract_text() or "") + "\n"

    linhas = texto_completo.split("\n")
    i = 0
    while i < len(linhas):
        linha = linhas[i].strip()
        match = re.match(
            r'^(\d+)\s+(\d+)\s+([A-ZÁÉÍÓÚÀÃÕÂÊÔÜÇ][A-ZÁÉÍÓÚÀÃÕÂÊÔÜÇa-záéíóúàãõâêôüç\s]+)$',
            linha
        )
        if match:
            nome = match.group(3).strip().upper()
            if i + 1 < len(linhas):
                prox = linhas[i + 1].strip()
                partes = prox.split("/")
                filiacao2    = partes[0].strip().upper() if len(partes) > 0 else ""
                cpf          = partes[1].strip()         if len(partes) > 1 else ""
                data_nasc_raw = partes[2].strip()        if len(partes) > 2 else ""
                data_nasc    = converter_data(data_nasc_raw)
                alunos.append({
                    "nome": nome,
                    "cpf": cpf,
                    "data_nascimento": data_nasc,
                    "filiacao2": filiacao2,
                })
                i += 2
                continue
        i += 1

    return alunos


MESES = {
    "janeiro": "01", "fevereiro": "02", "março": "03", "abril": "04",
    "maio": "05", "junho": "06", "julho": "07", "agosto": "08",
    "setembro": "09", "outubro": "10", "novembro": "11", "dezembro": "12"
}

def converter_data(texto):
    """Converte '11 de Julho de 2011' para '11/07/2011'."""
    if not texto:
        return ""
    texto = texto.strip().lower()
    match = re.match(r'(\d+)\s+de\s+(\w+)\s+de\s+(\d{4})', texto)
    if match:
        dia = match.group(1).zfill(2)
        mes = MESES.get(match.group(2), "00")
        ano = match.group(3)
        return f"{dia}/{mes}/{ano}"
    return texto


def buscar_por_nome(lista_cadastro, nome_aluno):
    """Busca cadastro pelo nome normalizado."""
    nome_norm = normalizar(nome_aluno)
    for cad in lista_cadastro:
        if normalizar(cad["nome"]) == nome_norm:
            return cad
    partes = nome_norm.split()
    if len(partes) >= 2:
        for cad in lista_cadastro:
            n = normalizar(cad["nome"]).split()
            if n and n[0] == partes[0] and n[-1] == partes[-1]:
                return cad
    return None


def gerar_csv(alunos_notas, disciplinas, cadastros, cabecalho_template, sep):
    """
    Monta CSV final: colunas fixas do template + disciplinas detectadas do PDF.
    """
    # Colunas fixas do template (ignora NOTA_*, ... e NOTA_N)
    cols_fixas = [
        c for c in cabecalho_template
        if not normalizar(c).startswith("NOTA") and normalizar(c) not in ("...", "")
    ]

    # Cabeçalho final: fixas + nomes reais das disciplinas
    cabecalho_final = cols_fixas + disciplinas

    def idx(candidatos):
        for cand in candidatos:
            for i, c in enumerate(cabecalho_final):
                if normalizar(c) == normalizar(cand):
                    return i
        return -1

    col_nome      = idx(["NOME DO ESTUDANTE", "NOME DO ALUNO"])
    col_situacao  = idx(["SITUACAO", "SITUAÇÃO"])
    col_cpf       = idx(["CPF"])
    col_nasc      = idx(["DATA DE NASCIMENTO"])
    col_filiacao2 = idx(["NOME DA FILIACAO 2", "NOME DA FILIAÇÃO 2"])

    output = io.StringIO()
    writer = csv.writer(output, delimiter=sep)
    writer.writerow(cabecalho_final)

    for aluno in alunos_notas:
        linha = [""] * len(cabecalho_final)

        if col_nome >= 0:
            linha[col_nome] = aluno["nome"]
        if col_situacao >= 0:
            linha[col_situacao] = aluno["situacao"]

        inicio_notas = len(cols_fixas)
        for i, nota in enumerate(aluno["notas"]):
            if inicio_notas + i < len(linha):
                linha[inicio_notas + i] = nota or ""

        cad = buscar_por_nome(cadastros, aluno["nome"])
        if cad:
            if col_cpf >= 0:
                linha[col_cpf] = cad.get("cpf", "")
            if col_nasc >= 0:
                linha[col_nasc] = cad.get("data_nascimento", "")
            if col_filiacao2 >= 0:
                linha[col_filiacao2] = cad.get("filiacao2", "")

        writer.writerow(linha)

    return "\ufeff" + output.getvalue()


# ============================================================
# ROTAS
# ============================================================

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "servico": "Extrator de Notas - Colégio Ativo"})


@app.route("/extrair", methods=["POST"])
def extrair():
    try:
        if "pdf_notas" not in request.files:
            return jsonify({"erro": "PDF de notas não enviado"}), 400
        if "csv_template" not in request.files:
            return jsonify({"erro": "CSV modelo não enviado"}), 400

        pdf_notas_bytes    = request.files["pdf_notas"].read()
        csv_template_bytes = request.files["csv_template"].read()
        turma              = request.form.get("turma", "turma")

        # Lê template CSV com múltiplos encodings
        csv_texto = None
        for enc in ["utf-8-sig", "utf-8", "latin-1", "cp1252"]:
            try:
                csv_texto = csv_template_bytes.decode(enc).strip()
                break
            except Exception:
                continue
        if csv_texto is None:
            return jsonify({"erro": "Não foi possível ler o CSV. Salve como UTF-8."}), 400

        linhas_csv = [l for l in csv_texto.split("\n") if l.strip()]
        sep        = ";" if ";" in linhas_csv[0] else ","
        cabecalho  = [c.strip() for c in linhas_csv[0].split(sep)]

        # Extrai notas + disciplinas do PDF (dinâmico)
        alunos_notas, disciplinas = extrair_notas_pdf(pdf_notas_bytes)
        if not alunos_notas:
            return jsonify({"erro": "Nenhum aluno encontrado no PDF de notas."}), 400

        # Extrai cadastro (opcional)
        cadastros = []
        if "pdf_cadastro" in request.files:
            pdf_cadastro_bytes = request.files["pdf_cadastro"].read()
            if pdf_cadastro_bytes:
                cadastros = extrair_cadastro_pdf(pdf_cadastro_bytes)

        # Gera CSV com disciplinas reais
        csv_final    = gerar_csv(alunos_notas, disciplinas, cadastros, cabecalho, sep)
        ano_letivo   = "2025"
        nome_arquivo = f"resultado_{turma.replace(' ', '_').lower()}_{ano_letivo}.csv"

        return Response(
            csv_final.encode("utf-8-sig"),
            mimetype="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{nome_arquivo}"',
                "Access-Control-Allow-Origin": "*"
            }
        )

    except Exception as e:
        return jsonify({"erro": str(e)}), 500


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
