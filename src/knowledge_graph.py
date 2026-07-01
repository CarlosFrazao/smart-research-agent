"""
knowledge_graph.py — Subsistema de Grafo de Conhecimento Semântico do SRA (Bloco 3.2)

Fornece extração de triplas (regex + LLM), persistência no KuzuDB e exportação nos formatos Turtle (RDF) e JSON.
"""
from __future__ import annotations

import logging
import re
import json
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

logger = logging.getLogger("knowledge_graph")

# Relações semânticas a detectar via padrões regex
RELATION_PATTERNS = {
    "founded_by":    r"\b([A-Za-z0-9_.\-]+(?:\s+[A-Z][A-Za-z0-9_.\-]*)*)\s+(?i:(?:was\s+|is\s+)?founded\s+by)\s+([A-Za-z0-9_.\-]+(?:\s+[A-Z][A-Za-z0-9_.\-]*)*)\b",
    "produces":      r"\b([A-Za-z0-9_.\-]+(?:\s+[A-Z][A-Za-z0-9_.\-]*)*)\s+(?i:produces?)\s+([A-Za-z0-9_.\-]+(?:\s+[A-Z][A-Za-z0-9_.\-]*)*)\b",
    "develops":      r"\b([A-Za-z0-9_.\-]+(?:\s+[A-Z][A-Za-z0-9_.\-]*)*)\s+(?i:develops?)\s+([A-Za-z0-9_.\-]+(?:\s+[A-Z][A-Za-z0-9_.\-]*)*)\b",
    "competes_with": r"\b([A-Za-z0-9_.\-]+(?:\s+[A-Z][A-Za-z0-9_.\-]*)*)\s+(?i:competes?\s+with)\s+([A-Za-z0-9_.\-]+(?:\s+[A-Z][A-Za-z0-9_.\-]*)*)\b",
    "acquired_by":   r"\b([A-Za-z0-9_.\-]+(?:\s+[A-Z][A-Za-z0-9_.\-]*)*)\s+(?i:(?:was\s+|is\s+)?acquired\s+by)\s+([A-Za-z0-9_.\-]+(?:\s+[A-Z][A-Za-z0-9_.\-]*)*)\b",
    "partners_with": r"\b([A-Za-z0-9_.\-]+(?:\s+[A-Z][A-Za-z0-9_.\-]*)*)\s+(?i:(?:partners?|partnered|collaborates?|collaborated)\s+with)\s+([A-Za-z0-9_.\-]+(?:\s+[A-Z][A-Za-z0-9_.\-]*)*)\b",
    "is_a":          r"\b([A-Za-z0-9_.\-]+(?:\s+[A-Z][A-Za-z0-9_.\-]*)*)\s+(?i:is\s+an?)\s+([A-Za-z0-9_.\-]+(?:\s+[A-Z][A-Za-z0-9_.\-]*)*)\b",
    "has_part":      r"\b([A-Za-z0-9_.\-]+(?:\s+[A-Z][A-Za-z0-9_.\-]*)*)\s+(?i:has)\s+([A-Za-z0-9_.\-]+(?:\s+[A-Z][A-Za-z0-9_.\-]*)*)\s+(?i:part)\b",
    "located_in":    r"\b([A-Za-z0-9_.\-]+(?:\s+[A-Z][A-Za-z0-9_.\-]*)*)\s+(?i:is\s+located\s+in)\s+([A-Za-z0-9_.\-]+(?:\s+[A-Z][A-Za-z0-9_.\-]*)*)\b",
    "created_by":    r"\b([A-Za-z0-9_.\-]+(?:\s+[A-Z][A-Za-z0-9_.\-]*)*)\s+(?i:(?:was\s+|is\s+)?created\s+by)\s+([A-Za-z0-9_.\-]+(?:\s+[A-Z][A-Za-z0-9_.\-]*)*)\b",
    "author_of":     r"\b([A-Za-z0-9_.\-]+(?:\s+[A-Z][A-Za-z0-9_.\-]*)*)\s+(?i:authored?|wrote?)\s+([A-Za-z0-9_.\-]+(?:\s+[A-Z][A-Za-z0-9_.\-]*)*)\b",
}


@dataclass
class Triple:
    subject: str
    relation: str
    object: str
    confidence: float
    source: str


class SemanticKnowledgeGraph:
    """
    Interface para gerenciar e interagir com o Grafo de Conhecimento Semântico.
    Conecta-se ao KuzuDB fornecido e gerencia as tabelas SemanticEntity e RELATION.
    """

    def __init__(self, kuzu_conn: Any = None, llm_client: Any = None):
        self.kuzu_conn = kuzu_conn
        self.llm = llm_client
        if self.kuzu_conn:
            self._init_schema()

    def _init_schema(self) -> None:
        """Inicializa as tabelas SemanticEntity e RELATION no KuzuDB se não existirem."""
        # Nota: as tabelas também são inicializadas no orvix_memory_v2._init_kuzu
        try:
            self.kuzu_conn.execute(
                "CREATE NODE TABLE SemanticEntity(name STRING, type STRING, PRIMARY KEY(name))"
            )
        except Exception as e:
            if "already exists" not in str(e).lower():
                logger.debug(f"KG: Tabela SemanticEntity já existe ou erro: {e}")

        try:
            self.kuzu_conn.execute(
                "CREATE REL TABLE RELATION(FROM SemanticEntity TO SemanticEntity, type STRING, confidence DOUBLE)"
            )
        except Exception as e:
            if "already exists" not in str(e).lower():
                logger.debug(f"KG: Tabela RELATION já existe ou erro: {e}")

    # ── Extração de Triplas ───────────────────────────────────────────────────

    def extract_triples(self, text: str) -> List[Triple]:
        """
        Extração heurística de triplas baseada em regex case-insensitive.
        """
        triples = []
        for rel_name, pattern in RELATION_PATTERNS.items():
            matches = re.findall(pattern, text)
            for m in matches:
                if len(m) == 2:
                    sub = m[0].strip()
                    obj = m[1].strip()
                    # Filtra falsos positivos simples (números isolados, pronomes comuns)
                    if self._is_valid_entity(sub) and self._is_valid_entity(obj):
                        triples.append(Triple(
                            subject=sub,
                            relation=rel_name,
                            object=obj,
                            confidence=0.75,
                            source="regex_heuristics"
                        ))
        return triples

    async def extract_triples_with_llm(self, text: str) -> List[Triple]:
        """
        Usa o cliente LLM para realizar extração semântica profunda de triplas.
        """
        if not self.llm:
            logger.warning("KG: extract_triples_with_llm falhou — LLMClient não fornecido.")
            return []

        prompt = (
            "Você é um extrator de triplas semânticas (sujeito, relação, objeto). Escreva em Português do Brasil.\n\n"
            "Analise o texto fornecido e extraia as relações semânticas importantes entre entidades de tecnologia, "
            "empresas, autores ou projetos.\n\n"
            "Relações suportadas:\n"
            "- founded_by (empresa fundada por pessoa/organização)\n"
            "- produces (empresa/projeto produz produto)\n"
            "- develops (desenvolvedor/empresa desenvolve projeto)\n"
            "- competes_with (projeto/empresa compete com outro)\n"
            "- acquired_by (empresa adquirida por outra)\n"
            "- partners_with (empresa parceira de outra)\n"
            "- is_a (entidade pertence a uma classe ou categoria)\n"
            "- has_part (entidade possui componente/parte)\n"
            "- located_in (entidade fica geograficamente localizada em)\n"
            "- created_by (projeto criado por pessoa/organização)\n"
            "- author_of (autor escreveu livro/artigo)\n\n"
            f"Texto: \"{text}\"\n\n"
            "Responda APENAS com um JSON contendo uma lista de objetos no seguinte formato (nada de texto antes ou depois):\n"
            "[\n"
            "  {\n"
            "    \"subject\": \"<sujeito>\",\n"
            "    \"relation\": \"<relação listada acima>\",\n"
            "    \"object\": \"<objeto>\",\n"
            "    \"confidence\": <0.0 a 1.0>\n"
            "  },\n"
            "  ...\n"
            "]\n"
        )
        try:
            raw = await self.llm.generate(prompt, temperature=0.2, max_tokens=600)
            
            # Limpa qualquer delimitador de código Markdown ```json
            clean_raw = raw.strip()
            if clean_raw.startswith("```"):
                # Remove primeira linha
                lines = clean_raw.splitlines()
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines[-1].strip() == "```":
                    lines = lines[:-1]
                clean_raw = "\n".join(lines).strip()

            start = clean_raw.find("[")
            if start == -1:
                return []
            
            data = json.loads(clean_raw[start:])
            triples = []
            for item in data:
                sub = item.get("subject", "").strip()
                rel = item.get("relation", "").strip()
                obj = item.get("object", "").strip()
                
                # Valida tipo de relação
                if rel in RELATION_PATTERNS and sub and obj:
                    triples.append(Triple(
                        subject=sub,
                        relation=rel,
                        object=obj,
                        confidence=float(item.get("confidence", 0.8)),
                        source="llm_extraction"
                    ))
            return triples
        except Exception as e:
            logger.warning(f"KG: extract_triples_with_llm falhou: {e}")
            return []

    # ── Persistência KuzuDB ───────────────────────────────────────────────────

    def add_triple(self, triple: Triple) -> None:
        """
        Adiciona a tripla semântica ao banco de grafos KuzuDB.
        Insere/atualiza os nós SemanticEntity e cria a aresta RELATION correspondente.
        """
        if not self.kuzu_conn:
            logger.debug("KG: KuzuDB não conectado, tripla não adicionada.")
            return

        try:
            # 1. Merge do sujeito
            sub_type = self._determine_entity_type(triple.subject, triple.relation, is_subject=True)
            self.kuzu_conn.execute(
                "MERGE (s:SemanticEntity {name: $name}) "
                "ON CREATE SET s.type = $type",
                {"name": triple.subject, "type": sub_type}
            )

            # 2. Merge do objeto
            obj_type = self._determine_entity_type(triple.object, triple.relation, is_subject=False)
            self.kuzu_conn.execute(
                "MERGE (o:SemanticEntity {name: $name}) "
                "ON CREATE SET o.type = $type",
                {"name": triple.object, "type": obj_type}
            )

            # 3. Verifica se a aresta já existe para evitar duplicações
            check_q = (
                "MATCH (s:SemanticEntity)-[r:RELATION]->(o:SemanticEntity) "
                "WHERE s.name = $sub AND o.name = $obj AND r.type = $rel "
                "RETURN r"
            )
            res = self.kuzu_conn.execute(check_q, {
                "sub": triple.subject,
                "obj": triple.object,
                "rel": triple.relation
            })
            if res.has_next():
                return  # Relação já cadastrada no grafo

            # 4. Cria a relação RELATION
            create_q = (
                "MATCH (s:SemanticEntity), (o:SemanticEntity) "
                "WHERE s.name = $sub AND o.name = $obj "
                "CREATE (s)-[r:RELATION {type: $rel, confidence: $conf}]->(o)"
            )
            self.kuzu_conn.execute(create_q, {
                "sub": triple.subject,
                "obj": triple.object,
                "rel": triple.relation,
                "conf": float(triple.confidence)
            })
            logger.debug(f"KG: Tripla adicionada: ({triple.subject}) -[{triple.relation}]-> ({triple.object})")
        except Exception as e:
            logger.warning(f"KG: Erro ao adicionar tripla ao KuzuDB: {e}")

    # ── Consultas no Grafo ────────────────────────────────────────────────────

    def query_graph(
        self,
        subject: Optional[str] = None,
        relation: Optional[str] = None,
        object: Optional[str] = None
    ) -> List[Triple]:
        """
        Consulta o grafo de relações semânticas e retorna uma lista de triplas.
        Filtros opcionais por subject, relation e object.
        """
        if not self.kuzu_conn:
            return []

        conditions = []
        params = {}

        if subject:
            conditions.append("s.name = $sub")
            params["sub"] = subject
        if relation:
            conditions.append("r.type = $rel")
            params["rel"] = relation
        if object:
            conditions.append("o.name = $obj")
            params["obj"] = object

        where_clause = " AND ".join(conditions)
        if where_clause:
            where_clause = "WHERE " + where_clause

        query = f"""
            MATCH (s:SemanticEntity)-[r:RELATION]->(o:SemanticEntity)
            {where_clause}
            RETURN s.name, r.type, o.name, r.confidence
        """

        triples = []
        try:
            res = self.kuzu_conn.execute(query, params)
            while res.has_next():
                row = res.get_next()
                triples.append(Triple(
                    subject=str(row[0]),
                    relation=str(row[1]),
                    object=str(row[2]),
                    confidence=float(row[3]),
                    source="kuzudb_query"
                ))
        except Exception as e:
            logger.warning(f"KG: query_graph falhou: {e}")

        return triples

    def get_related_entities(self, entity: str) -> List[str]:
        """
        Retorna nomes únicos de entidades diretamente ligadas a uma entidade.
        """
        if not self.kuzu_conn:
            return []

        query = """
            MATCH (s:SemanticEntity)-[r:RELATION]-(o:SemanticEntity)
            WHERE s.name = $name
            RETURN DISTINCT o.name
        """
        entities = []
        try:
            res = self.kuzu_conn.execute(query, {"name": entity})
            while res.has_next():
                row = res.get_next()
                entities.append(str(row[0]))
        except Exception as e:
            logger.warning(f"KG: get_related_entities falhou: {e}")
        return entities

    # ── Exportação ────────────────────────────────────────────────────────────

    def export_ttl(self) -> str:
        """
        Exporta as triplas semânticas cadastradas no formato Turtle (RDF).
        """
        triples = self.query_graph()
        lines = [
            "@prefix sra: <http://smart-research-agent.org/resource/> .",
            "@prefix srap: <http://smart-research-agent.org/property/> .",
            ""
        ]

        def clean_uri(val: str) -> str:
            clean = re.sub(r"[^a-zA-Z0-9_\-]", "_", val.replace(" ", "_"))
            return f"sra:{clean}"

        for t in triples:
            sub = clean_uri(t.subject)
            rel = f"srap:{t.relation}"
            obj = clean_uri(t.object)
            lines.append(f"{sub} {rel} {obj} .")

        return "\n".join(lines)

    def export_json(self) -> Dict[str, Any]:
        """
        Exporta as triplas no formato estruturado JSON de grafos (nós e links).
        """
        if not self.kuzu_conn:
            return {"nodes": [], "links": []}

        nodes_map = {}
        links = []

        try:
            # 1. Recupera todos os nós
            res_nodes = self.kuzu_conn.execute("MATCH (e:SemanticEntity) RETURN e.name, e.type")
            while res_nodes.has_next():
                row = res_nodes.get_next()
                nodes_map[str(row[0])] = {
                    "id": str(row[0]),
                    "type": str(row[1])
                }

            # 2. Recupera todos os links
            triples = self.query_graph()
            for t in triples:
                links.append({
                    "source": t.subject,
                    "target": t.object,
                    "type": t.relation,
                    "confidence": t.confidence
                })
        except Exception as e:
            logger.warning(f"KG: export_json falhou: {e}")

        return {
            "nodes": list(nodes_map.values()),
            "links": links
        }

    # ── Helpers internos ──────────────────────────────────────────────────────

    def _is_valid_entity(self, name: str) -> bool:
        """Valida se a string é uma entidade aceitável (tamanho, conteúdo)."""
        if not name or len(name) < 2:
            return False
        # Ignora se for puramente numérica
        if name.isdigit():
            return False
        # Ignora stopwords inglesas ou portuguesas comuns capturadas por acidente
        stopwords = {
            "this", "that", "these", "those", "there", "their", "where", "which",
            "what", "when", "with", "from", "into", "onto", "your", "they", "them",
            "este", "esta", "esse", "essa", "aquele", "aquela", "tudo", "nada", "quem"
        }
        if name.lower() in stopwords:
            return False
        return True

    def _determine_entity_type(self, entity_name: str, relation: str, is_subject: bool) -> str:
        """Heurística simples para classificar o tipo da entidade (Person, Company, Project, Product, Object)."""
        rel = relation.lower()
        if rel == "founded_by":
            return "Company" if is_subject else "Person"
        elif rel in ["produces", "develops"]:
            return "Company" if is_subject else "Project"
        elif rel == "competes_with":
            return "Project"
        elif rel == "acquired_by":
            return "Company"
        elif rel == "author_of":
            return "Person" if is_subject else "Product"
        elif rel in ["is_a", "located_in"]:
            return "Object"
        return "GenericEntity"
