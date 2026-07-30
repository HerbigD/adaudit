/** 与后端 graph/state.py 一一对应的类型。改动请同步两侧。 */

export type Route1 = "direct" | "search" | "human";
export type Route2 = "direct_verified" | "human";
export type HumanChoice = "original" | "prediction" | "manual";
export type AuditStatus =
  | "queued"
  | "running"
  | "direct"
  | "direct_verified"
  | "pending_human"
  | "done"
  | "failed";

export type LeafOrParent = "leaf" | "parent";

export interface Classification {
  product_name: string | null;
  brand: string | null;
  name_brand_identifiable: boolean;
  /** 广告画面主语言（ISO 639-1）与推断国家（ISO 3166-1 alpha-2） */
  ad_language: string;
  country: string | null;
  general_id: number;
  general_category: string;
  /** 粒度自适应：叶子未定时为 null，候选见 candidate_codes */
  specific_code: number | null;
  candidate_codes: number[];
  leaf_vs_parent: LeafOrParent;
  specific_confidence: number;
  general_confidence: number;
  reasoning: string;
  /** 引用的 Evidence.id（ev_001…），groundedness 靠它核到具体条目 */
  evidence_refs: string[];
  conflict: boolean;
  source: "vlm" | "adjudicator" | "human" | "cache";
  model: string | null;
  /** 结果产出方：mock-vlm / rule-fallback / gemini … */
  adapter: string | null;
}

export type Nutrient = "sugar" | "fat" | "fiber" | "sodium" | "protein";
export type SourceType = "official" | "ecommerce" | "nutrition_db" | "cache" | "other";

export interface NutrientValue {
  nutrient: Nutrient;
  value: number;
  unit: string;
  /** 统一到 g/100g 或 g/100ml；缺份量等换算不出时为 null */
  normalized: number | null;
  confidence: number;
}

export interface Evidence {
  id: string;
  product_query: string;
  source_url: string;
  source_title: string;
  source_type: SourceType;
  snippet: string;
  nutrients: NutrientValue[];
  /** 降级模式：没有营养面板时 LLM 给的类别倾向 */
  conclusion_hint: string | null;
  provenance: "web" | "cache";
  /** 3 = 去品牌查询命中，裁决时已降权 */
  query_tier: number;
  extracted_by: string;
  extracted_at: string;
  /** 缓存证据才有：auto = 自动沉淀未经人工核验；human_verified = 人工核过 */
  cache_provenance: "auto" | "human_verified" | null;
}

export interface StepTrace {
  node: string;
  status: "ok" | "skipped" | "fallback" | "error";
  ms: number;
  summary: string;
  adapter: string | null;
  queries_used: number;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
  fallback_reason: string | null;
  extra: Record<string, unknown>;
  at: number;
}

export interface Audit {
  id: string;
  batch_id: string | null;
  image_path: string;
  status: AuditStatus;
  route_1: Route1 | null;
  route_2: Route2 | null;
  human_choice: HumanChoice | null;
  created_at: string;
  updated_at: string;
  initial: Classification | null;
  revised: Classification | null;
  final: Classification | null;
  trace: StepTrace[] | null;
  reason?: string;
  evidence?: Evidence[];
}

export interface BatchStats {
  total: number;
  completed: number;
  general_distribution: Record<string, number>;
  specific_distribution: Record<string, number>;
  confidence_histogram: Record<string, number>;
  route_distribution: Record<string, number>;
  search_trigger_rate: number;
  human_review_rate: number;
  human_choice_distribution: Record<string, number>;
  original_adopted_rate: number;
  prediction_adopted_rate: number;
  hfss_share: number;
  parent_level_count: number;
  parent_level_share: number;
  adapters: Record<string, number>;
  taxonomy_version: string;
  cache: {
    products: number;
    total_hits: number;
    human_verified: number;
    superseded: number;
    backend: string;
  };
}

export interface Batch {
  id: string;
  name: string;
  status: string;
  created_at: string;
  report_md: string | null;   // 后端列名即 report_md
  stats: BatchStats;
  audits: Audit[];
  pending_human: number;
}

export interface TrendPoint {
  batch_id: string;
  name: string;
  created_at: string;
  human_review_rate: number;
  search_trigger_rate: number;
  cache_products: number;
  cache_hits: number;
}

export interface GeneralCategory {
  id: number;
  name_en: string;
  name_zh: string;
  label: string;
}

export interface SpecificCategory {
  code: number;
  parent_id: number;
  name_zh: string;
  name_en: string;
  description_zh: string;
  key_dimensions: string[];
  evidence_needed: string[];
  confusable_with: number[];
  confirmed: boolean;
}

export interface TaxonomyCascade {
  version: string;
  updated: string;
  confirmed_ratio: number;
  generals: GeneralCategory[];
  specifics: SpecificCategory[];
  confusing_pairs: { pair: [number, number]; note: string }[];
}
