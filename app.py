"""
Backend Flask - Extrator de Notas Colégio Ativo
Recebe PDFs via POST, extrai dados com pdfplumber (100% preciso)
e retorna CSV pronto para importar no SIGEDUC.
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

COLUNAS_IGNORAR = {
    "N", "Nº", "NUMERO", "ALUNO", "ALUNO(A)", "NOME", "SITUACAO",
    "SITUAÇÃO", "SITUACAO FINAL", "SITUAÇÃO FINAL", "FREQUENCIA",
    "FREQUÊNCIA", "FREQUENCIA ANUAL", "FREQUÊNCIA ANUAL",
    "CARGA HORARIA", "CARGA HORÁRIA", "TOTAL DE FALTAS", "F", ""
}

FREQUENCIA_PADRAO = "99"

# ============================================================
# FUNÇÕES AUXILIARES
# ============================================================

def normalizar(texto):
    if not texto:
        return ""
    nfkd = unicodedata.normalize('NFKD', str(texto))
    return "".join(c for c in nfkd if not unicodedata.combining(c)).upper().strip()


def limpar_nota(valor):
    if valor is None:
        return "-"
    v = str(valor).strip()
    if v in ("---", "", "0,0", "0.0"):
        return "-"
    v = v.replace(",", ".").strip()
    try:
        f = float(v)
        if 0.1 <= f <= 10.0:
            return str(f).replace(".", ",")
        return "-"
    except ValueError:
        return "-"


def detectar_disciplinas(cabecalho_pdf):
    """Detecta disciplinas e seus índices no cabeçalho da tabela."""
    disciplinas = []
    i = 0
    while i < len(cabecalho_pdf):
        cel = str(cabecalho_pdf[i] or "").strip()
        cel_norm = normalizar(cel)

        if cel_norm in {normalizar(x) for x in COLUNAS_IGNORAR}:
            i += 1
            continue

        if cel and len(cel) > 1:
            nome = " ".join(cel.split())
            # Cabeçalhos verticais no PDF SIGA são lidos invertidos
            nome = nome[::-1]
            disciplinas.append((nome, i))
            i += 2  # pula coluna de faltas
        else:
            i += 1

    return disciplinas


# ============================================================
# EXTRAÇÃO DO PDF DE NOTAS
# ============================================================

def extrair_notas_pdf(pdf_bytes):
    alunos = []
    disciplinas_detectadas = []

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            tabela = page.extract_table()
            if not tabela:
                continue

            # Detecta cabeçalho da tabela
            cabecalho_idx = None
            for idx, row in enumerate(tabela):
                if not row:
                    continue
                celulas_texto = [c for c in row if c and len(str(c).strip()) > 2]
                if len(celulas_texto) >= 5 and not str(row[0] or "").strip().isdigit():
                    cabecalho_idx = idx
                    break

            if cabecalho_idx is not None and not disciplinas_detectadas:
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

                notas = []
                for nome_disc, col_idx in disciplinas_detectadas:
                    if col_idx < len(row):
                        notas.append(limpar_nota(row[col_idx]))
                    else:
                        notas.append("-")

                alunos.append({
                    "nome": nome.upper(),
                    "situacao": situacao,
                    "notas": notas,
                })

    nomes_disciplinas = [d[0] for d in disciplinas_detectadas]
    return alunos, nomes_disciplinas


# ============================================================
# EXTRAÇÃO DO PDF DE CADASTRO
# Formato novo: Filiação 1 / Filiação 2 / Sexo / CPF / Data nascimento
# ============================================================

def extrair_cadastro_pdf(pdf_bytes):
    alunos = []

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        texto_completo = ""
        for page in pdf.pages:
            texto_completo += (page.extract_text() or "") + "\n"

    linhas = texto_completo.split("\n")
    i = 0
    while i < len(linhas):
        linha = linhas[i].strip()

        # Linha de aluno: número + matrícula + nome em maiúsculas
        match = re.match(
            r'^(\d+)\s+(\d+)\s+([A-ZÁÉÍÓÚÀÃÕÂÊÔÜÇ][A-ZÁÉÍÓÚÀÃÕÂÊÔÜÇa-záéíóúàãõâêôüç\s]+)$',
            linha
        )
        if match:
            nome = match.group(3).strip().upper()

            if i + 1 < len(linhas):
                prox = linhas[i + 1].strip()
                partes = [p.strip() for p in prox.split("/")]

                # Formato: Filiação1 / Filiação2 / Sexo / CPF / Data nascimento
                filiacao1    = partes[0].upper() if len(partes) > 0 else ""
                filiacao2    = partes[1].upper() if len(partes) > 1 else "-"
                sexo_raw     = partes[2].strip().upper() if len(partes) > 2 else ""
                sexo         = "Masculino" if sexo_raw == "M" else "Feminino" if sexo_raw == "F" else ""
                cpf          = partes[3]          if len(partes) > 3 else '""'
                data_raw     = partes[4]          if len(partes) > 4 else ""

                # CPF vazio → aspas duplas conforme exigência SIGEDUC
                cpf = cpf.strip()
                if not cpf:
                    cpf = '""'

                # Filiação 2 vazia → traço conforme exigência SIGEDUC
                if not filiacao2.strip() or filiacao2.strip() == "":
                    filiacao2 = "-"

                data_nasc = converter_data(data_raw)

                alunos.append({
                    "nome":           nome,
                    "filiacao1":      filiacao1,
                    "filiacao2":      filiacao2,
                    "sexo":           sexo,
                    "cpf":            cpf,
                    "data_nascimento": data_nasc,
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


# ============================================================
# GERAÇÃO DO CSV
# ============================================================

def gerar_csv(alunos_notas, disciplinas, cadastros, sep):
    """
    Gera CSV no formato exato exigido pelo SIGEDUC:
    CPF ; Nome do Estudante ; Data de Nascimento ; Sexo ;
    Nome da Filiação 1 ; Nome da Filiação 2 ; Código INEP ;
    Frequência ; Situação ; Nota_disc1 ; Nota_disc2 ; ...
    SEM cabeçalho (conforme exigência do sistema).
    """
    output = io.StringIO()
    writer = csv.writer(output, delimiter=sep)

    for aluno in alunos_notas:
        cad = buscar_por_nome(cadastros, aluno["nome"])

        cpf        = cad.get("cpf", '""')            if cad else '""'
        nome       = aluno["nome"]
        data_nasc  = cad.get("data_nascimento", "")  if cad else ""
        sexo       = cad.get("sexo", "")              if cad else ""
        filiacao1  = cad.get("filiacao1", "")         if cad else ""
        filiacao2  = cad.get("filiacao2", "-")        if cad else "-"
        inep       = "-"  # preenchido com traço conforme SIGEDUC
        frequencia = FREQUENCIA_PADRAO
        situacao   = aluno["situacao"]

        linha = [
            cpf,
            nome,
            data_nasc,
            sexo,
            filiacao1,
            filiacao2,
            inep,
            frequencia,
            situacao,
        ] + aluno["notas"]

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

        pdf_notas_bytes = request.files["pdf_notas"].read()
        turma           = request.form.get("turma", "turma")

        # Separador padrão SIGEDUC
        sep = ";"

        # Extrai notas + disciplinas do PDF
        alunos_notas, disciplinas = extrair_notas_pdf(pdf_notas_bytes)
        if not alunos_notas:
            return jsonify({"erro": "Nenhum aluno encontrado no PDF de notas."}), 400

        # Extrai cadastro (opcional)
        cadastros = []
        if "pdf_cadastro" in request.files:
            pdf_cadastro_bytes = request.files["pdf_cadastro"].read()
            if pdf_cadastro_bytes:
                cadastros = extrair_cadastro_pdf(pdf_cadastro_bytes)

        # CSV template não é mais necessário — ignora se enviado

        # Gera CSV sem cabeçalho (exigência SIGEDUC)
        csv_final    = gerar_csv(alunos_notas, disciplinas, cadastros, sep)
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
