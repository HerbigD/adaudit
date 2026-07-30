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

export interface Classification {
  product_name: string | null;
  brand: string | null;
  general_category: string;
  specific_code: number;
  specific_confidence: number;
  general_confidence: number;
  reasoning: string;
  alternative_code: number | null;
  name_or_brand_legible: boolean;
  evidence_refs: number[];
  conflict: boolean;
  source: "vlm" | "adjudicator" | "human" | "cache";
  model: string | null;
}

export interface Evidence {
  source: "cache" | "web";
  url: string | null;
  title: string | null;
  snippet: string | null;
  sugar_g: number | null;
  fat_g: number | null;
  sat_fat_g: number | null;
  fibre_g: number | null;
  salt_g: number | null;
  energy_kj: number | null;
  confidence: number;
}

export interface StepTrace {
  node: string;
  status: "ok" | "skipped" | "fallback" | "error";
  ms: number;
  summary: string;
  queries_used: number;
  cost_usd: number;
  fallback_reason: string | null;
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
  cache: { products: number; total_hits: number; backend: string };
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

export interface SpecificCategory {
  code: number;
  name: string;
  general: string;
}
