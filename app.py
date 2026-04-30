"""
Backend Flask - Extrator de Notas Colégio Ativo
Recebe PDFs via POST, extrai dados com pdfplumber (100% preciso)
e retorna CSV pronto para importar no SIGA.
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import pdfplumber
import io
import csv
import re
import unicodedata

app = Flask(__name__)
CORS(app)  # Permite requisições do HTML na Hostinger

# ============================================================
# CONFIGURAÇÕES
# ============================================================

# Índices das colunas de NOTA na tabela do PDF
# Estrutura: Nº | Nome | PORT | F | MAT | F | BIO | F | HIS | F | GEO | F | ART | F | ING | F | PROD | F | EDF | F | QUI | F | FIS | F | FREQ | SITUACAO
# Onde F = faltas (ignorar)
COLUNAS_NOTAS = [2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22]

DISCIPLINAS = [
    "PORTUGUES",
    "MATEMATICA",
    "BIOLOGIA_CIENCIAS",
    "HISTORIA",
    "GEOGRAFIA",
    "ARTES",
    "INGLES",
    "PRODUCAO_TEXTUAL",
    "EDUCACAO_FISICA",
    "QUIMICA",
    "FISICA",
]

# ============================================================
# FUNÇÕES DE EXTRAÇÃO
# ============================================================

def normalizar(texto):
    """Remove acentos e converte para maiúsculas para comparação."""
    if not texto:
        return ""
    nfkd = unicodedata.normalize('NFKD', str(texto))
    return "".join(c for c in nfkd if not unicodedata.combining(c)).upper().strip()


def limpar_nota(valor):
    """Converte nota brasileira (7,5) para float ou retorna None."""
    if valor is None:
        return None
    v = str(valor).strip()
    if v in ("---", "", "0,0", "0.0"):
        return None
    # Remove espaços e converte vírgula para ponto
    v = v.replace(",", ".").strip()
    try:
        f = float(v)
        # Notas válidas ficam entre 0.1 e 10.0
        if 0.1 <= f <= 10.0:
            return str(f).replace(".", ",")
        return None
    except ValueError:
        return None


def extrair_notas_pdf(pdf_bytes):
    """Extrai alunos e notas do PDF de ata de resultados usando pdfplumber."""
    alunos = []

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            tabela = page.extract_table()
            if not tabela:
                continue

            for row in tabela:
                if not row or not row[0]:
                    continue

                # Linha de aluno: primeira coluna é número inteiro
                num = str(row[0]).strip()
                if not num.isdigit():
                    continue

                nome = str(row[1]).strip() if len(row) > 1 and row[1] else ""
                if not nome or len(nome) < 3:
                    continue

                # Situação final: última coluna
                situacao = str(row[-1]).strip() if row[-1] else ""

                # Extrai notas nas colunas definidas
                notas = []
                for col_idx in COLUNAS_NOTAS:
                    if col_idx < len(row):
                        notas.append(limpar_nota(row[col_idx]))
                    else:
                        notas.append(None)

                alunos.append({
                    "nome": nome.upper(),
                    "situacao": situacao,
                    "notas": notas,
                })

    return alunos


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

        # Detecta linha de aluno: começa com número seguido de matrícula e nome
        match = re.match(r'^(\d+)\s+(\d+)\s+([A-ZÁÉÍÓÚÀÃÕÂÊÔÜÇ][A-ZÁÉÍÓÚÀÃÕÂÊÔÜÇa-záéíóúàãõâêôüç\s]+)$', linha)
        if match:
            nome = match.group(3).strip().upper()

            # Próxima linha: Filiação2 / CPF / Data nascimento
            if i + 1 < len(linhas):
                prox = linhas[i + 1].strip()
                partes = prox.split("/")

                filiacao2 = partes[0].strip().upper() if len(partes) > 0 else ""
                cpf = partes[1].strip() if len(partes) > 1 else ""
                data_nasc_raw = partes[2].strip() if len(partes) > 2 else ""

                # Converte data "11 de Julho de 2011" → "11/07/2011"
                data_nasc = converter_data(data_nasc_raw)

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
        dia  = match.group(1).zfill(2)
        mes  = MESES.get(match.group(2), "00")
        ano  = match.group(3)
        return f"{dia}/{mes}/{ano}"
    return texto


def buscar_por_nome(lista_cadastro, nome_aluno):
    """Busca cadastro pelo nome normalizado."""
    nome_norm = normalizar(nome_aluno)
    for cad in lista_cadastro:
        if normalizar(cad["nome"]) == nome_norm:
            return cad
    # Tenta match parcial (primeiro + último nome)
    partes = nome_norm.split()
    if len(partes) >= 2:
        for cad in lista_cadastro:
            n = normalizar(cad["nome"]).split()
            if n and n[0] == partes[0] and n[-1] == partes[-1]:
                return cad
    return None


def gerar_csv(alunos_notas, cadastros, cabecalho_template, sep):
    """
    Monta o CSV final combinando notas + cadastro.
    Cabeçalho: colunas fixas do template + disciplinas reais.
    """
    # Colunas fixas (ignora NOTA_1, ..., NOTA_N do template)
    cols_fixas = [c for c in cabecalho_template
                  if not c.upper().startswith("NOTA") and c.strip() not in ("...", "")]

    # Cabeçalho final: fixas + disciplinas
    cabecalho_final = cols_fixas + DISCIPLINAS

    # Índices das colunas fixas importantes
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

        # Nome e situação
        if col_nome >= 0:
            linha[col_nome] = aluno["nome"]
        if col_situacao >= 0:
            linha[col_situacao] = aluno["situacao"]

        # Notas nas colunas das disciplinas
        inicio_notas = len(cols_fixas)
        for i, nota in enumerate(aluno["notas"]):
            if inicio_notas + i < len(linha):
                linha[inicio_notas + i] = nota or ""

        # Dados cadastrais
        cad = buscar_por_nome(cadastros, aluno["nome"])
        if cad:
            if col_cpf >= 0:
                linha[col_cpf] = cad.get("cpf", "")
            if col_nasc >= 0:
                linha[col_nasc] = cad.get("data_nascimento", "")
            if col_filiacao2 >= 0:
                linha[col_filiacao2] = cad.get("filiacao2", "")

        writer.writerow(linha)

    return "\ufeff" + output.getvalue()  # BOM UTF-8 para Excel


# ============================================================
# ROTAS
# ============================================================

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "servico": "Extrator de Notas - Colégio Ativo"})


@app.route("/extrair", methods=["POST"])
def extrair():
    """
    Recebe:
      - pdf_notas: arquivo PDF da ata de resultados (obrigatório)
      - pdf_cadastro: arquivo PDF de dados dos alunos (opcional)
      - csv_template: arquivo CSV modelo (obrigatório)
      - turma: nome da turma (texto)

    Retorna: CSV pronto para importar no SIGA
    """
    try:
        # Valida arquivos obrigatórios
        if "pdf_notas" not in request.files:
            return jsonify({"erro": "PDF de notas não enviado"}), 400
        if "csv_template" not in request.files:
            return jsonify({"erro": "CSV modelo não enviado"}), 400

        pdf_notas_bytes    = request.files["pdf_notas"].read()
        csv_template_bytes = request.files["csv_template"].read()
        turma              = request.form.get("turma", "turma")

        # Lê template CSV — tenta vários encodings
        csv_texto = None
        for enc in ["utf-8-sig", "utf-8", "latin-1", "cp1252"]:
            try:
                csv_texto = csv_template_bytes.decode(enc).strip()
                break
            except Exception:
                continue
        if csv_texto is None:
            return jsonify({"erro": "Não foi possível ler o arquivo CSV. Tente salvar como UTF-8."}), 400
        linhas_csv  = [l for l in csv_texto.split("\n") if l.strip()]
        sep         = ";" if ";" in linhas_csv[0] else ","
        cabecalho   = [c.strip() for c in linhas_csv[0].split(sep)]

        # Extrai notas do PDF de ata (pdfplumber — 100% preciso)
        alunos_notas = extrair_notas_pdf(pdf_notas_bytes)
        if not alunos_notas:
            return jsonify({"erro": "Nenhum aluno encontrado no PDF de notas. Verifique o arquivo."}), 400

        # Extrai cadastro (opcional)
        cadastros = []
        if "pdf_cadastro" in request.files:
            pdf_cadastro_bytes = request.files["pdf_cadastro"].read()
            if pdf_cadastro_bytes:
                cadastros = extrair_cadastro_pdf(pdf_cadastro_bytes)

        # Gera CSV
        csv_final = gerar_csv(alunos_notas, cadastros, cabecalho, sep)

        ano_letivo  = "2025"
        nome_arquivo = f"resultado_{turma.replace(' ', '_').lower()}_{ano_letivo}.csv"

        from flask import Response
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
