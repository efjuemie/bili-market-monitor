import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Scatter,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { TooltipContentProps } from "recharts";
import { API_BASE_URL, APP_VERSION } from "./config";

type MonitorAccessStatus = "approved" | "not_requested" | "pending" | "rejected";
type MonitorAccessRequest = { reason: string; submitted_at: string; reviewed_at?: string | null; reviewed_by_admin_id?: string | null; review_note?: string | null };
type User = { id: string; username: string; email: string | null; email_verified: boolean; role: string; is_active: boolean; created_at: string; monitor_access_status?: MonitorAccessStatus };
type Product = { cluster_id: number; title: string; cover_url: string | null; detail_url: string; available: boolean; current_price: string | null; reference_price: string | null; purchase_button_text: string | null; delivery_mode: string | null; recent_avg_price: string | null; recent_deal_price: string | null; recent_deal_time_text: string | null; last_checked_at: string | null; last_success_at: string | null; last_error: string | null };
type Favorite = { id: string; cluster_id: number; product: Product; target_price: string | null; notify_enabled: boolean; check_interval_seconds: number; next_check_at: string | null; last_evaluated_at: string | null; last_condition_met: boolean; last_alert_price: string | null; last_alert_at: string | null; last_manual_refresh_at: string | null };
type HistoryRollup = { window_start?: string | null; window_end?: string | null; bucket_start?: string | null; bucket_end?: string | null; period_end?: string | null; last_price?: string | number | null; min_price?: string | number | null; max_price?: string | number | null; last_reference_price?: string | number | null; sample_count?: number | null; available_count?: number | null; last_available?: boolean | null; resolution_seconds?: number | null; status?: string | null };
type HistoryPoint = { observed_at?: string; available?: boolean; current_price?: string | null; reference_price?: string | null; point_type?: string | null; aggregation?: HistoryRollup | null; rollup?: HistoryRollup | null } & HistoryRollup;
type HistoryRange = "1h" | "6h" | "24h" | "7d" | "30d" | "90d";
type HistoryState = { range: HistoryRange; points: HistoryPoint[]; loadedAt: string; expanded: boolean; pending: boolean };
type ChartPoint = HistoryPoint & { timestamp: number; price: number | null; reference: number | null; interaction: number };
type ApiErrorDetails = ApiErrorDetail[] | Record<string, unknown>;
type ApiError = { error?: { code?: string; message?: string; details?: ApiErrorDetails } };

type ApiErrorDetail = { loc?: Array<string | number>; msg?: string; type?: string };

class ApiRequestError extends Error {
  readonly status: number | null;
  readonly code?: string;
  readonly details?: ApiErrorDetails;

  constructor(message: string, options: { status?: number | null; code?: string; details?: ApiErrorDetails } = {}) {
    super(message);
    this.name = "ApiRequestError";
    this.status = options.status ?? null;
    this.code = options.code;
    this.details = options.details;
  }
}

const intervals = [
  [10, "10 秒"], [30, "30 秒"], [60, "1 分钟"], [180, "3 分钟"],
  [300, "5 分钟"], [600, "10 分钟"], [1800, "30 分钟"], [3600, "1 小时"],
] as const;
const BOSS_URL = "https://www.bili-market-boss.top/#/";
const EMAIL_VERIFICATION_MESSAGE = "开启邮件提醒前，请先前往“个人资料”绑定并验证通知邮箱。";
const historyRanges: Array<[HistoryRange, string]> = [["1h", "1 小时"], ["6h", "6 小时"], ["24h", "24 小时"], ["7d", "7 天"], ["30d", "30 天"], ["90d", "90 天"]];

function isEmailVerificationRequired(error: unknown) {
  return error instanceof ApiRequestError && error.code === "EMAIL_VERIFICATION_REQUIRED";
}

function isMonitorAccessError(error: unknown) {
  return error instanceof ApiRequestError && [
    "MONITOR_ACCESS_NOT_APPROVED",
    "MONITOR_ACCESS_REQUEST_ALREADY_PENDING",
    "MONITOR_ACCESS_ALREADY_APPROVED",
  ].includes(error.code || "");
}

function monitorAccessStatusFromError(error: unknown, fallback: MonitorAccessStatus = "not_requested"): MonitorAccessStatus {
  if (!(error instanceof ApiRequestError)) return fallback;
  const details = error.details;
  const value = !Array.isArray(details) && typeof details?.monitor_access_status === "string"
    ? details.monitor_access_status
    : undefined;
  if (value === "approved" || value === "not_requested" || value === "pending" || value === "rejected") return value;
  if (error.code === "MONITOR_ACCESS_REQUEST_ALREADY_PENDING") return "pending";
  if (error.code === "MONITOR_ACCESS_ALREADY_APPROVED") return "approved";
  return fallback;
}

function apiUrl(path: string) {
  const base = API_BASE_URL.replace(/\/+$/, "") || "/api/v1";
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  const apiPrefix = "/api/v1";
  const route = /\/api\/v1$/i.test(base) && (normalizedPath === apiPrefix || normalizedPath.startsWith(`${apiPrefix}/`))
    ? normalizedPath.slice(apiPrefix.length) || "/"
    : normalizedPath;
  return route === "/" && base !== "/" ? base : `${base}${route}`;
}

function ProductImage({ clusterId, src, title }: { clusterId: number; src: string | null; title: string }) {
  const imageUrl = src?.trim() ? apiUrl(`/bili-market/products/${encodeURIComponent(String(clusterId))}/cover`) : null;
  const [failedUrl, setFailedUrl] = useState<string | null>(null);

  useEffect(() => {
    setFailedUrl(null);
  }, [imageUrl]);

  if (!imageUrl || failedUrl === imageUrl) {
    return <div className="product-image-placeholder" role="img" aria-label={`${title}商品预览图`}>暂无图片</div>;
  }

  return <img src={imageUrl} alt={`${title}商品预览图`} onError={() => setFailedUrl(imageUrl)} />;
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (!headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  let response: Response;
  try {
    response = await fetch(apiUrl(path), { credentials: "include", ...init, headers });
  } catch (error) {
    console.error("API request failed", { path, error });
    throw new ApiRequestError("网络请求失败，请稍后重试。", { status: null });
  }
  let data: ApiError = {};
  if (!response.ok) {
    try {
      data = (await response.json()) as ApiError;
    } catch (error) {
      console.error("API error response was not JSON", { path, status: response.status, error });
    }
    const details = data.error?.details;
    throw new ApiRequestError(apiErrorMessage(response.status, path, data.error?.message, data.error?.code), {
      status: response.status,
      code: data.error?.code,
      details,
    });
  }
  try {
    return await response.json() as T;
  } catch (error) {
    console.error("API success response was not JSON", { path, error });
    throw new ApiRequestError("服务响应异常，请稍后重试。", { status: response.status });
  }
}

function isSafeChineseMessage(message: string | undefined): message is string {
  if (!message) return false;
  const value = message.trim();
  if (value.length === 0 || value.length > 160 || /[\r\n]/.test(value)) return false;
  if (!/[\u4e00-\u9fff]/.test(value)) return false;
  return !/failed to fetch|networkerror|typeerror|domexception|internal server error|the string did not match/i.test(value);
}

function apiErrorMessage(status: number, path: string, backendMessage?: string, code?: string) {
  if (code === "USERNAME_ALREADY_EXISTS" || (status === 409 && path.includes("/auth/register"))) return "该用户名已被使用，请更换后重试。";
  if (code === "EMAIL_SERVICE_UNAVAILABLE") return "验证码暂时无法发送，请稍后重试。";
  if (code === "EMAIL_SEND_FAILED") return "验证码发送失败，请稍后重试。";
  if (code === "MONITOR_ACCESS_NOT_APPROVED") return "当前账号尚未获得监控功能权限，请前往个人资料提交申请。";
  if (code === "MONITOR_ACCESS_REQUEST_ALREADY_PENDING") return "监控功能申请正在审核中，请等待管理员处理。";
  if (code === "MONITOR_ACCESS_ALREADY_APPROVED") return "监控功能已经开通，无需重复申请。";
  if (code === "ADMIN_NOTIFICATION_EMAIL_UNAVAILABLE") return "该用户没有可用的已验证邮箱，无法同步发送邮件。";
  if (code === "PRODUCT_NOT_FOUND" || (status === 404 && path.includes("/bili-market/products/"))) return "未找到该商品，请确认商品 ID 是否正确。";
  if (status === 401) return "登录状态已失效，请重新登录。";
  if (status === 429) return "操作过于频繁，请稍后再试。";
  if (status >= 500 && status <= 599) return "服务暂时不可用，请稍后重试。";
  if (status === 400 || status === 422) return isSafeChineseMessage(backendMessage) ? backendMessage : "输入内容有误，请检查后重试。";
  if (status === 404) return "请求的资源不存在，请检查后重试。";
  return isSafeChineseMessage(backendMessage) ? backendMessage : "请求失败，请稍后重试。";
}

function toUserMessage(error: unknown, fallback = "请求失败，请稍后重试。") {
  if (error instanceof ApiRequestError) return error.message;
  if (error instanceof Error && isSafeChineseMessage(error.message)) return error.message;
  if (typeof error === "string" && isSafeChineseMessage(error)) return error;
  if (error) console.error("Unexpected UI error", error);
  return fallback;
}

function storedProductErrorMessage(value: string) {
  return isSafeChineseMessage(value) ? value : "暂时无法获取B站市集商品信息，请稍后重试。";
}

type AuthFieldErrors = { username?: string; password?: string; confirmPassword?: string };

function formatInterval(seconds: number) { return intervals.find(([value]) => value === seconds)?.[1] || `${seconds} 秒`; }
function formatTime(value: string | null | undefined) {
  if (!value) return "尚未更新";
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toLocaleString("zh-CN", { hour12: false }) : "尚未更新";
}

function formatChartTime(value: number, range: HistoryRange) {
  const options: Intl.DateTimeFormatOptions = range === "1h" || range === "6h"
    ? { hour: "2-digit", minute: "2-digit", hour12: false }
    : range === "24h"
      ? { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }
      : { month: "numeric", day: "numeric" };
  return new Date(value).toLocaleString("zh-CN", options);
}

function formatPrice(value: number | null) {
  return value === null || !Number.isFinite(value) ? "—" : `¥${value.toFixed(2)}`;
}

function rollupFor(point: HistoryPoint) { return point.rollup || point.aggregation || null; }

function numberFrom(value: string | number | null | undefined) {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function isRollupPoint(point: HistoryPoint) {
  const rollup = rollupFor(point);
  const hasInterval = Boolean(point.period_end && point.observed_at && point.period_end !== point.observed_at);
  return Boolean(point.point_type === "rollup" || rollup || point.window_start || point.window_end || point.bucket_start || point.bucket_end || hasInterval || point.available_count !== undefined && point.available_count !== null || point.last_available !== undefined && point.last_available !== null || point.resolution_seconds !== undefined && point.resolution_seconds !== null || point.sample_count !== undefined && point.sample_count !== 1 || point.last_price !== undefined && point.last_price !== null || point.min_price !== undefined && point.min_price !== null || point.max_price !== undefined && point.max_price !== null);
}

function pointObservedAt(point: HistoryPoint) {
  const rollup = rollupFor(point);
  return point.observed_at ?? rollup?.window_start ?? rollup?.bucket_start ?? "";
}

function pointStatus(point: HistoryPoint) {
  const status = String(rollupFor(point)?.status ?? point.status ?? "").toLowerCase();
  if (status.includes("sold") || status.includes("unavailable") || status.includes("售罄")) return "已售罄";
  if (status.includes("available") || status.includes("可购买")) return "可购买";
  return (point.available ?? rollupFor(point)?.last_available) === false ? "已售罄" : "可购买";
}

function pointPrice(point: HistoryPoint) {
  const rollup = rollupFor(point);
  return numberFrom(rollup?.last_price ?? point.last_price ?? point.current_price);
}

function pointMinimum(point: HistoryPoint) {
  const rollup = rollupFor(point);
  return numberFrom(rollup?.min_price ?? point.min_price ?? pointPrice(point));
}

function pointMaximum(point: HistoryPoint) {
  const rollup = rollupFor(point);
  return numberFrom(rollup?.max_price ?? point.max_price ?? pointPrice(point));
}

function HistoryPointDetails({ point }: { point: ChartPoint }) {
  const rollup = rollupFor(point);
  if (isRollupPoint(point)) {
    const start = rollup?.window_start ?? rollup?.bucket_start ?? point.window_start ?? point.bucket_start ?? pointObservedAt(point);
    const end = rollup?.window_end ?? rollup?.bucket_end ?? rollup?.period_end ?? point.window_end ?? point.bucket_end ?? point.period_end ?? pointObservedAt(point);
    const sampleCount = rollup?.sample_count ?? point.sample_count;
    return <><strong>时间区间：{formatTime(start)}{start !== end ? ` ～ ${formatTime(end)}` : ""}</strong><span>状态：{pointStatus(point)}</span><span>最后价：{formatPrice(pointPrice(point))}</span><span>最低：{formatPrice(pointMinimum(point))} · 最高：{formatPrice(pointMaximum(point))}</span><span>样本：{sampleCount ?? "—"}</span></>;
  }
  return <><strong>{formatTime(pointObservedAt(point))}</strong><span>状态：{pointStatus(point)}</span><span>当前价格：{formatPrice(point.price)}</span><span>参考价：{formatPrice(point.reference)}</span></>;
}

function HistoryTooltip({ active, payload }: Partial<TooltipContentProps<number, string>>) {
  if (!active || !payload?.length) return null;
  const point = payload.find(entry => entry?.payload)?.payload as ChartPoint | undefined;
  if (!point) return null;
  return <div className="history-tooltip"><HistoryPointDetails point={point} /></div>;
}

function HistoryChart({ points, range, onRangeChange, loading }: { points: HistoryPoint[]; range: HistoryRange; onRangeChange: (range: HistoryRange) => void; loading: boolean }) {
  const [selectedPoint, setSelectedPoint] = useState<ChartPoint | null>(null);
  const [coarsePointer, setCoarsePointer] = useState(false);
  useEffect(() => setSelectedPoint(null), [range]);
  useEffect(() => {
    const media = window.matchMedia("(pointer: coarse)");
    setCoarsePointer(media.matches);
  }, []);
  const chartPoints = points.map(point => ({
    ...point,
    timestamp: new Date(pointObservedAt(point)).getTime(),
    price: pointPrice(point),
    reference: numberFrom(point.reference_price ?? rollupFor(point)?.last_reference_price),
  })).filter(point => Number.isFinite(point.timestamp)) as Omit<ChartPoint, "interaction">[];
  const priced = chartPoints.filter(point => point.price !== null && Number.isFinite(point.price));
  const allSoldOut = chartPoints.length > 0 && priced.length === 0;
  const chartMin = priced.length ? Math.min(...priced.map(point => point.price as number)) : 0;
  const chartMax = priced.length ? Math.max(...priced.map(point => point.price as number)) : 0;
  const domain: [number | string, number | string] = chartMin === chartMax ? [Math.max(0, chartMin - 1), chartMax + 1] : ["auto", "auto"];
  const chartMinTime = chartPoints.length ? Math.min(...chartPoints.map(point => point.timestamp)) : 0;
  const chartMaxTime = chartPoints.length ? Math.max(...chartPoints.map(point => point.timestamp)) : 0;
  const timePadding = chartMinTime === chartMaxTime ? 30 * 60 * 1000 : 0;
  const timeDomain: [number | string, number | string] = chartPoints.length ? [chartMinTime - timePadding, chartMaxTime + timePadding] : ["dataMin", "dataMax"];
  const interactionPoints: ChartPoint[] = chartPoints.map(point => ({ ...point, interaction: chartMin }));
  const dot = priced.length <= 24 ? { r: 3, fill: "#00aeec", strokeWidth: 0 } : priced.length <= 48 ? { r: 1.5, fill: "#00aeec", strokeWidth: 0 } : false;

  return <section className="history-panel" aria-label="价格历史">
    <div className="history-toolbar">
      <div><strong>价格历史</strong><span className="hint">{loading ? "正在加载…" : `${chartPoints.length} 个记录`}</span></div>
      <div className="history-range" role="group" aria-label="历史时间范围">{historyRanges.map(([value, label]) => <button key={value} type="button" className={range === value ? "active" : ""} aria-pressed={range === value} onClick={() => onRangeChange(value)} disabled={loading}>{label}</button>)}</div>
    </div>
    {!chartPoints.length ? <p className="history-empty">暂无该时间范围内的价格历史记录。</p> : allSoldOut ? <p className="history-empty">该时间范围内全部售罄，没有可绘制的价格点。</p> : <>
      <div className="history-chart" role="img" aria-label="价格历史走势图，点击或触摸数据点可查看详情">
        <ResponsiveContainer width="100%" height={250} minWidth={0}>
          <LineChart data={chartPoints} margin={{ top: 12, right: 12, left: 4, bottom: 8 }} onClick={state => {
            const rawIndex = state?.activeTooltipIndex;
            const indexValue = rawIndex === undefined || rawIndex === null ? Number.NaN : Number(rawIndex);
            const index = Number.isInteger(indexValue) && indexValue >= 0 && indexValue < interactionPoints.length ? indexValue : null;
            const point = index === null ? undefined : interactionPoints[index];
            if (point) setSelectedPoint(point);
          }}>
            <CartesianGrid stroke="#e8eef5" strokeDasharray="3 3" />
            <XAxis dataKey="timestamp" type="number" scale="time" domain={timeDomain} tickFormatter={value => formatChartTime(Number(value), range)} tick={{ fontSize: 11, fill: "#8994a8" }} minTickGap={28} />
            <YAxis dataKey="price" domain={domain} tickFormatter={value => `¥${Number(value).toFixed(0)}`} tick={{ fontSize: 11, fill: "#8994a8" }} width={48} allowDataOverflow={false} />
            <Tooltip content={<HistoryTooltip />} trigger={coarsePointer ? "click" : "hover"} cursor={{ stroke: "#b9dcea", strokeDasharray: "4 4" }} />
            <Line type="monotone" dataKey="price" name="当前价格" stroke="#00aeec" strokeWidth={2.5} dot={dot} activeDot={{ r: 6 }} connectNulls={false} isAnimationActive={false} />
            <Scatter data={interactionPoints} dataKey="interaction" fill="rgba(0,0,0,0.001)" stroke="transparent" isAnimationActive={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>
      {selectedPoint && <div className="history-touch-detail" aria-live="polite"><HistoryPointDetails point={selectedPoint} /></div>}
      <div className="chart-labels"><span>最低 {formatPrice(chartMin)}</span><span>{priced.length} 个可购买价格点</span><span>最高 {formatPrice(chartMax)}</span></div>
    </>}
  </section>;
}

type SiteNotification = { id: string | number; kind?: string | null; severity?: string | null; title: string; body: string; action_url?: string | null; created_at: string; read?: boolean; read_at?: string | null };
type NotificationRequestKey = "count" | "list";

function notificationIsRead(item: SiteNotification) { return Boolean(item.read || item.read_at); }

function notificationPage(actionUrl: string | null | undefined) {
  if (!actionUrl || !actionUrl.startsWith("/") || actionUrl.startsWith("//")) return null;
  try {
    const parsed = new URL(actionUrl, window.location.origin);
    if (parsed.origin !== window.location.origin) return null;
    const pages: Record<string, string> = {
      "/": "home", "/favorites": "favorites", "/profile": "profile", "/tutorial": "tutorial",
      "/admin": "admin", "/login": "login", "/forgot": "forgot", "/reset-password": "reset-password",
    };
    return pages[parsed.pathname] || (/^\/bili-market\/products\/\d+$/.test(parsed.pathname) ? "home" : null);
  } catch {
    return null;
  }
}

function BellIcon() {
  return <svg className="notification-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9" /><path d="M13.73 21a2 2 0 0 1-3.46 0" /></svg>;
}

function NotificationBell({ onNavigate }: { onNavigate: (page: string, actionUrl?: string) => void }) {
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<SiteNotification[]>([]);
  const [unreadCount, setUnreadCount] = useState(0);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState("");
  const mountedRef = useRef(true);
  const requestIdsRef = useRef<Record<NotificationRequestKey, number>>({ count: 0, list: 0 });
  const controllersRef = useRef<Partial<Record<NotificationRequestKey, AbortController>>>({});

  function beginNotificationRequest(key: NotificationRequestKey) {
    controllersRef.current[key]?.abort();
    const controller = new AbortController();
    const requestId = (requestIdsRef.current[key] || 0) + 1;
    requestIdsRef.current[key] = requestId;
    controllersRef.current[key] = controller;
    return { controller, requestId };
  }

  function isCurrentNotificationRequest(key: NotificationRequestKey, requestId: number, controller: AbortController) {
    return mountedRef.current && requestIdsRef.current[key] === requestId && !controller.signal.aborted;
  }

  const loadUnread = useCallback(async () => {
    if (!mountedRef.current || document.visibilityState === "hidden") return;
    const request = beginNotificationRequest("count");
    try {
      const data = await api<{ unread_count?: number; count?: number }>("/notifications/unread-count", { signal: request.controller.signal });
      if (isCurrentNotificationRequest("count", request.requestId, request.controller)) setUnreadCount(Math.max(0, Number(data.unread_count ?? data.count ?? 0)));
    } catch (error) {
      if (isCurrentNotificationRequest("count", request.requestId, request.controller)) console.warn("Unable to refresh notification count", error);
    }
  }, []);

  const loadList = useCallback(async () => {
    if (!mountedRef.current) return;
    const request = beginNotificationRequest("list");
    setLoading(true);
    setMessage("");
    try {
      const data = await api<{ items?: SiteNotification[]; notifications?: SiteNotification[]; unread_count?: number }>("/notifications?limit=30", { signal: request.controller.signal });
      if (!isCurrentNotificationRequest("list", request.requestId, request.controller)) return;
      setItems(data.items || data.notifications || []);
      if (data.unread_count !== undefined) setUnreadCount(Math.max(0, Number(data.unread_count)));
    } catch (error) {
      if (isCurrentNotificationRequest("list", request.requestId, request.controller)) setMessage(toUserMessage(error));
    } finally {
      if (isCurrentNotificationRequest("list", request.requestId, request.controller)) setLoading(false);
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    const refresh = () => void loadUnread();
    const onVisibilityChange = () => { if (document.visibilityState === "visible") void loadUnread(); };
    window.addEventListener("bmm:notifications-refresh", refresh);
    document.addEventListener("visibilitychange", onVisibilityChange);
    void loadUnread();
    const timer = window.setInterval(() => void loadUnread(), 45_000);
    return () => {
      mountedRef.current = false;
      window.clearInterval(timer);
      (Object.keys(controllersRef.current) as NotificationRequestKey[]).forEach(key => controllersRef.current[key]?.abort());
      window.removeEventListener("bmm:notifications-refresh", refresh);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [loadUnread]);

  async function markRead(item: SiteNotification) {
    if (notificationIsRead(item)) return true;
    try {
      await api(`/notifications/${encodeURIComponent(String(item.id))}/read`, { method: "POST" });
      if (!mountedRef.current) return false;
      setItems(values => values.map(value => value.id === item.id ? { ...value, read: true, read_at: new Date().toISOString() } : value));
      setUnreadCount(value => Math.max(0, value - 1));
      return true;
    } catch (error) {
      if (mountedRef.current) setMessage(toUserMessage(error));
      return false;
    }
  }

  async function markAllRead() {
    try {
      await api("/notifications/read-all", { method: "POST" });
      if (!mountedRef.current) return;
      setItems(values => values.map(value => ({ ...value, read: true, read_at: value.read_at || new Date().toISOString() })));
      setUnreadCount(0);
    } catch (error) {
      if (mountedRef.current) setMessage(toUserMessage(error));
    }
  }

  async function openItem(item: SiteNotification) {
    if (!await markRead(item)) return;
    const page = notificationPage(item.action_url);
    if (page) { setOpen(false); onNavigate(page, item.action_url || undefined); }
  }

  return <div className="notification-center">
    <button type="button" className="notification-button" aria-label={unreadCount ? `通知，${unreadCount} 条未读` : "通知"} aria-expanded={open} onClick={() => { setOpen(value => !value); if (!open) void loadList(); }}>
      <BellIcon />{unreadCount > 0 && <span className="notification-badge">{unreadCount > 99 ? "99+" : unreadCount}</span>}
    </button>
    {open && <section className="notification-popover" aria-label="通知中心">
      <div className="notification-heading"><strong>通知中心</strong><button type="button" className="table-button" onClick={() => void markAllRead()} disabled={!unreadCount}>全部已读</button></div>
      {message && <p className="error notification-message">{message}</p>}
      {loading ? <p className="notification-empty">正在加载…</p> : items.length === 0 ? <p className="notification-empty">暂时没有通知。</p> : <div className="notification-list">{items.map(item => <button type="button" className={`notification-item ${notificationIsRead(item) ? "read" : "unread"}`} key={item.id} onClick={() => void openItem(item)}><span className="notification-item-title">{item.title}</span><span className="notification-item-body">{item.body}</span><span className="notification-item-meta">{formatTime(item.created_at)}{item.action_url ? " · 查看详情" : ""}</span></button>)}</div>}
    </section>}
  </div>;
}

function Header({ user, page, setPage, onLogout, onNotificationNavigate }: { user: User | null; page: string; setPage: (value: string) => void; onLogout: () => void; onNotificationNavigate: (page: string, actionUrl?: string) => void }) {
  return <header className="topbar"><div className="brand" onClick={() => setPage("home")} role="button" tabIndex={0}><span className="brand-mark">¥</span><span><strong>B站市集好价提示系统</strong><small>Bili Market Monitor</small></span></div><nav><button className={page === "home" ? "active" : ""} onClick={() => setPage("home")}>查询商品</button><button className={page === "tutorial" ? "active" : ""} onClick={() => setPage("tutorial")}>使用教程</button>{user && <button className={page === "favorites" ? "active" : ""} onClick={() => setPage("favorites")}>我的收藏</button>}{user && <button className={page === "profile" ? "active" : ""} onClick={() => setPage("profile")}>个人资料</button>}{user?.role === "admin" && <button className={page === "admin" ? "active" : ""} onClick={() => setPage("admin")}>管理后台</button>}</nav><div className="account">{user ? <><NotificationBell onNavigate={onNotificationNavigate} /><span className="user-chip">{user.username}</span><button className="ghost small" onClick={onLogout}>退出</button></> : <button className="primary small" onClick={() => setPage("login")}>登录 / 注册</button>}</div></header>;
}

type MonitorModalKind = "email" | "not_requested" | "pending" | "rejected";

function monitorModalKind(error: unknown, user: User | null): MonitorModalKind | null {
  if (isEmailVerificationRequired(error)) return "email";
  if (!isMonitorAccessError(error)) return null;
  const status = monitorAccessStatusFromError(error, user?.monitor_access_status || "not_requested");
  if (status === "pending") return "pending";
  if (status === "rejected") return "rejected";
  return "not_requested";
}

function MonitorRequirementModal({ kind, onPrimary, onClose }: { kind: MonitorModalKind; onPrimary: () => void; onClose: () => void }) {
  const modalRef = useRef<HTMLElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const onCloseRef = useRef(onClose);
  const copy: Record<MonitorModalKind, { title: string; body: string; primary: string; secondary: string }> = {
    email: { title: "请先验证通知邮箱", body: "开启自动监控前，需要先在个人资料中绑定并验证通知邮箱。", primary: "前往个人资料", secondary: "取消" },
    not_requested: { title: "监控功能尚未开通", body: "新注册账号需要先提交监控功能申请，经管理员审核通过后才能开启自动监控。", primary: "前往个人资料申请", secondary: "取消" },
    pending: { title: "监控申请审核中", body: "你的申请已经提交，请等待管理员审核。审核通过后即可开启自动监控。", primary: "查看申请状态", secondary: "知道了" },
    rejected: { title: "监控功能尚未开通", body: "当前申请暂未通过，你可以在个人资料中查看审核说明并重新提交申请。", primary: "重新申请", secondary: "取消" },
  };
  const value = copy[kind];
  useEffect(() => { onCloseRef.current = onClose; }, [onClose]);
  useEffect(() => {
    const previousFocus = document.activeElement as HTMLElement | null;
    closeButtonRef.current?.focus();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); onCloseRef.current(); return; }
      if (event.key !== "Tab") return;
      const focusable = modalRef.current?.querySelectorAll<HTMLElement>("button, [href], input, select, textarea, [tabindex]:not([tabindex=\"-1\"]):not([disabled])");
      if (!focusable?.length) { event.preventDefault(); modalRef.current?.focus(); return; }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;
      if (!modalRef.current?.contains(active)) { event.preventDefault(); first.focus(); }
      else if (event.shiftKey && active === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && active === last) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      previousFocus?.focus();
    };
  }, []);
  return <div className="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="monitor-requirement-title" aria-describedby="monitor-requirement-body" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}><section ref={modalRef} className="modal-card" tabIndex={-1}><span className="eyebrow">MONITOR ACCESS</span><h2 id="monitor-requirement-title">{value.title}</h2><p id="monitor-requirement-body">{value.body}</p><div className="modal-actions"><button ref={closeButtonRef} className="primary" onClick={onPrimary}>{value.primary}</button><button className="button ghost" onClick={onClose}>{value.secondary}</button></div></section></div>;
}

function AuthPanel({ mode, onSuccess, setMode }: { mode: "login" | "register"; onSuccess: (user: User) => void; setMode: (mode: "login" | "register" | "forgot") => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [message, setMessage] = useState("");
  const [fieldErrors, setFieldErrors] = useState<AuthFieldErrors>({});
  const [busy, setBusy] = useState(false);

  function updateField(field: keyof AuthFieldErrors, value: string) {
    if (field === "username") setUsername(value);
    if (field === "password") setPassword(value);
    if (field === "confirmPassword") setConfirmPassword(value);
    setFieldErrors(errors => ({ ...errors, [field]: undefined }));
    setMessage("");
  }

  function validate() {
    const errors: AuthFieldErrors = {};
    const normalizedUsername = username.trim();
    if (!normalizedUsername) errors.username = "请输入用户名。";
    else if (mode === "register" && (normalizedUsername.length < 3 || normalizedUsername.length > 32)) errors.username = "用户名长度需要为3～32个字符。";
    if (!password) errors.password = "请输入密码。";
    else if (mode === "register" && (password.length < 8 || password.length > 128)) errors.password = "密码长度需要为8～128个字符。";
    if (mode === "register" && password !== confirmPassword) errors.confirmPassword = "两次输入的密码不一致。";
    setFieldErrors(errors);
    return errors;
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setMessage("");
    const errors = validate();
    if (Object.keys(errors).length > 0) return;
    setBusy(true);
    try {
      const data = await api<{ user: User }>(`/auth/${mode}`, { method: "POST", body: JSON.stringify({ username: username.trim(), password }) });
      onSuccess(data.user);
    } catch (error) {
      const userMessage = toUserMessage(error);
      setMessage(userMessage);
      if (mode === "register" && error instanceof ApiRequestError && (error.status === 409 || error.code === "USERNAME_ALREADY_EXISTS")) {
        setFieldErrors(errors => ({ ...errors, username: userMessage }));
      }
    } finally {
      setBusy(false);
    }
  }

  return <section className="auth-card"><div className="section-heading"><span className="eyebrow">账户</span><h1>{mode === "login" ? "欢迎回来" : "创建你的监控账户"}</h1><p>{mode === "login" ? "登录后管理收藏和低价提醒。" : "注册后即可保存商品和监控设置。"}</p></div><form onSubmit={submit} noValidate className="stack"><label><span>用户名</span><input value={username} onChange={e => updateField("username", e.target.value)} minLength={mode === "register" ? 3 : undefined} maxLength={32} required aria-invalid={Boolean(fieldErrors.username)} aria-describedby={fieldErrors.username ? "auth-username-error" : mode === "register" ? "auth-username-hint" : undefined} className={fieldErrors.username ? "input-error" : ""} />{mode === "register" && <span id="auth-username-hint" className="field-hint">3～32个字符，可自由组合中英文、字母、数字及常见字符；首尾空格会自动忽略。用户名不区分英文字母大小写进行判重。</span>}{fieldErrors.username && <span id="auth-username-error" className="field-error" role="alert">{fieldErrors.username}</span>}</label><label><span>密码</span><input type="password" value={password} onChange={e => updateField("password", e.target.value)} minLength={mode === "register" ? 8 : undefined} maxLength={128} required aria-invalid={Boolean(fieldErrors.password)} aria-describedby={fieldErrors.password ? "auth-password-error" : mode === "register" ? "auth-password-hint" : undefined} className={fieldErrors.password ? "input-error" : ""} />{mode === "register" && <span id="auth-password-hint" className="field-hint">8～128个字符，可使用字母、数字和符号；建议避免使用容易猜到的密码。</span>}{fieldErrors.password && <span id="auth-password-error" className="field-error" role="alert">{fieldErrors.password}</span>}</label>{mode === "register" && <label><span>确认密码</span><input type="password" value={confirmPassword} onChange={e => updateField("confirmPassword", e.target.value)} minLength={8} maxLength={128} required aria-invalid={Boolean(fieldErrors.confirmPassword)} aria-describedby={fieldErrors.confirmPassword ? "auth-confirm-password-error" : undefined} className={fieldErrors.confirmPassword ? "input-error" : ""} />{fieldErrors.confirmPassword && <span id="auth-confirm-password-error" className="field-error" role="alert">{fieldErrors.confirmPassword}</span>}</label>}{message && <p className="error" role="alert" aria-live="polite">{message}</p>}<button className="primary full" disabled={busy}>{busy ? "处理中…" : mode === "login" ? "登录" : "注册并登录"}</button></form><div className="auth-links"><button onClick={() => { setFieldErrors({}); setMessage(""); setMode(mode === "login" ? "register" : "login"); }}>{mode === "login" ? "还没有账户？注册" : "已有账户？登录"}</button>{mode === "login" && <button onClick={() => setMode("forgot")}>忘记密码</button>}</div></section>;
}

function ProductCard({ product, user, onFavorite, onRefresh, onGoToProfile }: { product: Product; user: User | null; onFavorite: (target: string, interval: number, notify: boolean) => Promise<void>; onRefresh?: () => Promise<void>; onGoToProfile?: () => void }) {
  const [target, setTarget] = useState(""); const [interval, setIntervalValue] = useState(300); const [notify, setNotify] = useState(false); const [busy, setBusy] = useState(false); const [message, setMessage] = useState(""); const [emailVerificationRequired, setEmailVerificationRequired] = useState(false); const [monitorModal, setMonitorModal] = useState<MonitorModalKind | null>(null);
  function changeNotify(value: boolean) {
    if (!value) { setNotify(false); return; }
    const knownStatus = user?.monitor_access_status;
    const kind = !user?.email_verified ? "email" : knownStatus && knownStatus !== "approved" ? knownStatus === "pending" ? "pending" : knownStatus === "rejected" ? "rejected" : "not_requested" : null;
    if (kind) { setNotify(false); setMonitorModal(kind); return; }
    setNotify(true);
  }
  async function save() {
    setBusy(true); setMessage(""); setEmailVerificationRequired(false);
    try { await onFavorite(target, interval, notify); setMessage("已保存到收藏"); }
    catch (error) {
      const kind = monitorModalKind(error, user);
      if (kind) { setNotify(false); setMonitorModal(kind); }
      if (isEmailVerificationRequired(error)) { setEmailVerificationRequired(true); setMessage(EMAIL_VERIFICATION_MESSAGE); }
      else setMessage(toUserMessage(error));
    } finally { setBusy(false); }
  }
  function goToProfile() { setMonitorModal(null); onGoToProfile?.(); }
  return <><article className="product-card"><div className="product-top"><div className="cover-wrap"><ProductImage clusterId={product.cluster_id} src={product.cover_url} title={product.title} /></div><div className="product-info"><div className="product-id">ClsId / clusterId · {product.cluster_id}</div><h2>{product.title}</h2><div className="price-line"><span className={product.available ? "price" : "price muted"}>{product.available && product.current_price ? `¥${product.current_price}` : "暂不可购买"}</span>{product.reference_price && <span className="reference">参考价 ¥{product.reference_price}</span>}</div><div className="status-row"><span className={product.available ? "status available" : "status sold"}>{product.available ? "可购买" : product.purchase_button_text || "已售罄"}</span>{product.delivery_mode && <span>{product.delivery_mode}</span>}{product.recent_deal_price && <span>最近成交 ¥{product.recent_deal_price}</span>}</div>{product.last_error && <p className="notice warning">本次更新失败，展示最近一次成功数据：{storedProductErrorMessage(product.last_error)}</p>}<p className="updated">最近成功更新：{formatTime(product.last_success_at)}</p></div></div><div className="product-actions"><a className="button outline" href={product.detail_url} target="_blank" rel="noopener noreferrer">前往 B 站市集 ↗</a>{onRefresh && <button className="button outline" onClick={async () => { setBusy(true); setMessage(""); try { await onRefresh(); setMessage("已刷新"); } catch (error) { setMessage(toUserMessage(error)); } finally { setBusy(false); } }} disabled={busy}>立即刷新</button>}</div>{user && <div className="monitor-panel"><div className="monitor-fields"><label>目标价格（元）<input inputMode="decimal" placeholder="例如 110.00" value={target} onChange={e => setTarget(e.target.value)} /></label><label>检查频率<select value={interval} onChange={e => setIntervalValue(Number(e.target.value))}>{intervals.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><label className="switch-label"><input type="checkbox" checked={notify} onChange={e => changeNotify(e.target.checked)} /><span>启用邮件提醒</span></label></div><p className="hint">更高频率可以更快发现价格变化，但实际检查时间可能受到 B 站接口限流、网络和系统保护策略影响。</p><button className="primary" onClick={save} disabled={busy}>{busy ? "保存中…" : "收藏并保存设置"}</button>{message && <div className={emailVerificationRequired ? "notice warning" : "inline-message"} role={emailVerificationRequired ? "alert" : undefined}>{message}</div>}{emailVerificationRequired && onGoToProfile && <button className="button outline" onClick={onGoToProfile}>前往个人资料</button>}</div>}</article>{monitorModal && <MonitorRequirementModal kind={monitorModal} onPrimary={goToProfile} onClose={() => setMonitorModal(null)} />}</>;
}

function SearchPage({ user, onRequireLogin, onOpenProfile, onOpenTutorial, initialClusterId }: { user: User | null; onRequireLogin: () => void; onOpenProfile: () => void; onOpenTutorial: () => void; initialClusterId?: string | null }) {
  const [clusterId, setClusterId] = useState(""); const [product, setProduct] = useState<Product | null>(null); const [loading, setLoading] = useState(false); const [message, setMessage] = useState("");
  const mountedRef = useRef(true);
  const requestIdRef = useRef(0);
  const controllerRef = useRef<AbortController | null>(null);
  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; controllerRef.current?.abort(); };
  }, []);
  const lookupProduct = useCallback(async (normalizedId: string) => {
    if (!mountedRef.current) return;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    const requestId = ++requestIdRef.current;
    setMessage("");
    setLoading(true);
    try {
      const data = await api<Product>(`/bili-market/products/${encodeURIComponent(normalizedId)}`, { signal: controller.signal });
      if (mountedRef.current && requestIdRef.current === requestId && !controller.signal.aborted) setProduct(data);
    } catch (error) {
      if (mountedRef.current && requestIdRef.current === requestId && !controller.signal.aborted) {
        setMessage(toUserMessage(error));
        throw error;
      }
    } finally {
      if (mountedRef.current && requestIdRef.current === requestId && !controller.signal.aborted) setLoading(false);
    }
  }, []);
  useEffect(() => {
    if (!initialClusterId) return;
    setClusterId(initialClusterId);
    void lookupProduct(initialClusterId).catch(() => undefined);
  }, [initialClusterId, lookupProduct]);
  async function search(event: FormEvent) { event.preventDefault(); const normalizedId = clusterId.trim(); if (!/^\d+$/.test(normalizedId)) { setMessage("请输入正确的商品ID，仅支持纯数字。"); return; } setProduct(null); await lookupProduct(normalizedId).catch(() => undefined); }
  async function favorite(target: string, interval: number, notify: boolean) { if (!user) { onRequireLogin(); return; } const value = target.trim() ? Number(target) : null; if (value !== null && (!Number.isFinite(value) || value <= 0)) throw new Error("目标价格必须是大于 0 的数字"); await api(`/bili-market/favorites`, { method: "POST", body: JSON.stringify({ cluster_id: product?.cluster_id, target_price: value, check_interval_seconds: interval, notify_enabled: notify }) }); }
  async function refresh() { if (!product) return; await lookupProduct(String(product.cluster_id)); }
  return <main className="page"><section className="hero"><span className="eyebrow">BILI MARKET MONITOR · v{APP_VERSION}</span><h1>先看清价格，再决定要不要出手。</h1><p>粘贴 B 站市集商品的 ClsId，查询当前最低可购买价，并设置自己的目标价格提醒。</p><form className="search-form" onSubmit={search}><input aria-label="商品 ID" value={clusterId} onChange={e => { setClusterId(e.target.value); setMessage(""); }} placeholder="输入商品 ID，例如 10000002733" inputMode="numeric" aria-invalid={Boolean(message)} aria-describedby={message ? "search-error" : undefined} className={message ? "input-error" : ""} /><button className="primary" disabled={loading}>{loading ? "查询中…" : "查询商品"}</button></form>{message && <p id="search-error" className="error search-error" role="alert" aria-live="polite">{message}</p>}<p className="privacy-note">价格监控直接使用 B 站市集接口；查询结果会保留最近一次成功数据。</p></section>{product && <ProductCard product={product} user={user} onFavorite={favorite} onRefresh={refresh} onGoToProfile={onOpenProfile} /> }<section className="help-card"><div><span className="eyebrow">CLS ID 教程</span><h2>还没有商品 ID？</h2><p>打开 BiliMarketBoss，搜索目标商品并先收藏它，再进入“收藏”页复制商品的 ClsId。</p></div><div className="help-actions"><a className="button primary" href={BOSS_URL} target="_blank" rel="noopener noreferrer">打开 BiliMarketBoss ↗</a><button className="button outline" onClick={onOpenTutorial}>查看详细教程</button></div></section>{!user && <section className="signin-prompt"><div><h3>保存收藏并接收提醒</h3><p>注册账户后可以收藏最多 20 件商品，并设置每件商品独立的检查频率。</p></div><button className="button outline" onClick={onRequireLogin}>登录 / 注册</button></section>}</main>;
}

function countdownSeconds(nextCheckAt: string | null, nowMs: number) {
  if (!nextCheckAt) return null;
  const timestamp = Date.parse(nextCheckAt);
  if (!Number.isFinite(timestamp)) return null;
  return Math.max(0, Math.ceil((timestamp - nowMs) / 1000));
}

function FavoritesPage({ user, onProductRefresh, onOpenProfile }: { user: User; onProductRefresh: () => void; onOpenProfile: () => void }) {
  const [items, setItems] = useState<Favorite[]>([]);
  const [history, setHistory] = useState<Record<string, HistoryState>>({});
  const [message, setMessage] = useState("");
  const [emailVerificationRequired, setEmailVerificationRequired] = useState(false);
  const [monitorModal, setMonitorModal] = useState<MonitorModalKind | null>(null);
  const [refreshingId, setRefreshingId] = useState<string | null>(null);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const itemsRef = useRef<Favorite[]>([]);
  const historyRef = useRef<Record<string, HistoryState>>({});
  const historyRequestIdsRef = useRef<Record<string, number>>({});
  const mountedRef = useRef(true);
  const favoritesLoadingRef = useRef(false);

  useEffect(() => { historyRef.current = history; }, [history]);
  useEffect(() => { itemsRef.current = items; }, [items]);
  useEffect(() => {
    const timer = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      Object.keys(historyRequestIdsRef.current).forEach(itemId => {
        historyRequestIdsRef.current[itemId] = (historyRequestIdsRef.current[itemId] || 0) + 1;
      });
    };
  }, []);

  const loadHistoryData = useCallback(async (itemId: string, clusterId: number, range: HistoryRange) => {
    if (!mountedRef.current) return;
    const requestId = (historyRequestIdsRef.current[itemId] || 0) + 1;
    historyRequestIdsRef.current[itemId] = requestId;
    const previous = historyRef.current[itemId];
    historyRef.current = {
      ...historyRef.current,
      [itemId]: {
        range,
        points: previous?.points || [],
        loadedAt: previous?.loadedAt || "",
        expanded: true,
        pending: true,
      },
    };
    setHistory(values => {
      const previousState = values[itemId];
      return {
        ...values,
        [itemId]: {
          range,
          points: previousState?.points || [],
          loadedAt: previousState?.loadedAt || "",
          expanded: true,
          pending: true,
        },
      };
    });
    try {
      const data = await api<{ items: HistoryPoint[] }>(`/bili-market/products/${clusterId}/history?range=${range}&max_points=240`);
      if (!mountedRef.current || historyRequestIdsRef.current[itemId] !== requestId) return;
      setHistory(values => {
        const current = values[itemId];
        if (!current?.expanded) return values;
        return { ...values, [itemId]: { range, points: data.items, loadedAt: new Date().toISOString(), expanded: true, pending: false } };
      });
    } catch (error) {
      if (!mountedRef.current || historyRequestIdsRef.current[itemId] !== requestId) return;
      setHistory(values => {
        const current = values[itemId];
        if (!current?.expanded) return values;
        return { ...values, [itemId]: { ...current, pending: false } };
      });
      setMessage(toUserMessage(error));
    }
  }, []);

  function hideHistory(itemId: string) {
    historyRequestIdsRef.current[itemId] = (historyRequestIdsRef.current[itemId] || 0) + 1;
    const current = historyRef.current[itemId];
    if (current) historyRef.current = { ...historyRef.current, [itemId]: { ...current, expanded: false, pending: false } };
    setHistory(values => {
      const currentState = values[itemId];
      if (!currentState) return values;
      return { ...values, [itemId]: { ...currentState, expanded: false, pending: false } };
    });
  }

  const loadFavorites = useCallback(async () => {
    if (!mountedRef.current || favoritesLoadingRef.current) return;
    favoritesLoadingRef.current = true;
    try {
      const data = await api<{ items: Favorite[] }>("/bili-market/favorites");
      if (!mountedRef.current) return;
      const previousById = new Map(itemsRef.current.map(item => [item.id, item]));
      setItems(data.items);
      itemsRef.current = data.items;
      const historyRefreshes = data.items.flatMap(item => {
        const previous = previousById.get(item.id);
        const expanded = historyRef.current[item.id];
        return previous && expanded?.expanded && previous.last_evaluated_at !== item.last_evaluated_at
          ? [{ item, range: expanded.range }]
          : [];
      });
      await Promise.all(historyRefreshes.map(({ item, range }) => loadHistoryData(item.id, item.cluster_id, range)));
    } catch (error) {
      if (mountedRef.current) setMessage(toUserMessage(error));
    } finally {
      favoritesLoadingRef.current = false;
    }
  }, [loadHistoryData]);

  useEffect(() => {
    let timer: number | undefined;
    const stopPolling = () => {
      if (timer !== undefined) window.clearInterval(timer);
      timer = undefined;
    };
    const startPolling = () => {
      stopPolling();
      if (document.visibilityState === "hidden") return;
      void loadFavorites();
      timer = window.setInterval(() => void loadFavorites(), 4000);
    };
    const onVisibilityChange = () => {
      if (document.visibilityState === "hidden") stopPolling();
      else startPolling();
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    startPolling();
    return () => {
      stopPolling();
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [loadFavorites]);

  async function update(item: Favorite, patch: Record<string, unknown>) {
    const enabling = patch.notify_enabled === true;
    if (enabling) {
      const knownStatus = user.monitor_access_status;
      const kind = !user.email_verified ? "email" : knownStatus && knownStatus !== "approved" ? knownStatus === "pending" ? "pending" : knownStatus === "rejected" ? "rejected" : "not_requested" : null;
      if (kind) { setMonitorModal(kind); return; }
    }
    try {
      const next = await api<Favorite>(`/bili-market/favorites/${item.cluster_id}`, { method: "PATCH", body: JSON.stringify(patch) });
      setItems(values => values.map(value => value.id === item.id ? next : value));
      itemsRef.current = itemsRef.current.map(value => value.id === item.id ? next : value);
    } catch (error) {
      const kind = monitorModalKind(error, user);
      if (kind) {
        if (enabling) setItems(values => values.map(value => value.id === item.id ? { ...value, notify_enabled: false } : value));
        setMonitorModal(kind);
      }
      if (isEmailVerificationRequired(error)) { setEmailVerificationRequired(true); setMessage(EMAIL_VERIFICATION_MESSAGE); }
      else setMessage(toUserMessage(error));
    }
  }

  async function remove(item: Favorite) {
    if (!window.confirm("确定删除这个收藏吗？")) return;
    try {
      await api(`/bili-market/favorites/${item.cluster_id}`, { method: "DELETE" });
      historyRequestIdsRef.current[item.id] = (historyRequestIdsRef.current[item.id] || 0) + 1;
      const currentHistory = historyRef.current[item.id];
      if (currentHistory) {
        const nextHistory = { ...historyRef.current };
        delete nextHistory[item.id];
        historyRef.current = nextHistory;
      }
      setItems(values => values.filter(value => value.id !== item.id));
      itemsRef.current = itemsRef.current.filter(value => value.id !== item.id);
      setHistory(values => { const next = { ...values }; delete next[item.id]; return next; });
    } catch (error) { setMessage(toUserMessage(error)); }
  }

  async function refresh(item: Favorite) {
    setRefreshingId(item.id);
    try {
      const result = await api<Product>(`/bili-market/favorites/${item.cluster_id}/refresh`, { method: "POST" });
      const updated = { ...item, product: result, last_manual_refresh_at: new Date().toISOString() };
      setItems(values => values.map(value => value.id === item.id ? updated : value));
      itemsRef.current = itemsRef.current.map(value => value.id === item.id ? updated : value);
      const expanded = historyRef.current[item.id];
      if (expanded?.expanded) await loadHistoryData(item.id, item.cluster_id, expanded.range);
      onProductRefresh();
    } catch (error) { setMessage(toUserMessage(error)); }
    finally { setRefreshingId(null); }
  }

  function toggleHistory(item: Favorite) {
    if (historyRef.current[item.id]?.expanded) {
      hideHistory(item.id);
      return;
    }
    void loadHistoryData(item.id, item.cluster_id, "24h");
  }

  function changeHistoryRange(item: Favorite, range: HistoryRange) {
    void loadHistoryData(item.id, item.cluster_id, range);
  }

  return <><main className="page"><div className="page-heading"><div><span className="eyebrow">MONITORING</span><h1>我的收藏</h1><p>每件商品可以独立设置目标价、邮件提醒和检查频率。</p><p className="hint load-advice">请尽量按实际需求添加监控商品，并为非紧急商品选择较低的检查频率；网站负载能力有限，请勿为所有商品统一设置过高监控频率。</p></div><span className="count-pill">{items.length} / 20</span></div>{(!user.email_verified || emailVerificationRequired) && <div className="notice warning email-verification-notice" role="alert"><span>{EMAIL_VERIFICATION_MESSAGE}</span><button className="button outline" onClick={onOpenProfile}>前往个人资料</button></div>}{message && !emailVerificationRequired && <p className="error">{message}</p>}{items.length === 0 ? <div className="empty"><h2>还没有收藏商品</h2><p>回到查询页，粘贴商品 ID 后即可保存。</p></div> : <div className="favorite-grid">{items.map(item => { const historyState = history[item.id]; const historyExpanded = historyState?.expanded === true; const countdown = item.notify_enabled ? countdownSeconds(item.next_check_at, nowMs) : null; return <article className="favorite-card" key={item.id}><div className="favorite-head"><div className="mini-cover"><ProductImage clusterId={item.cluster_id} src={item.product.cover_url} title={item.product.title} /></div><div><h2>{item.product.title}</h2><p className="product-id">{item.cluster_id} · {item.product.available ? `¥${item.product.current_price || "—"}` : "已售罄"}</p></div></div><div className="favorite-settings"><label>目标价<input value={item.target_price || ""} placeholder="未设置" onChange={e => setItems(values => values.map(v => v.id === item.id ? { ...v, target_price: e.target.value } : v))} onBlur={e => update(item, { target_price: e.target.value ? Number(e.target.value) : null })} /></label><label>检查频率<select value={item.check_interval_seconds} onChange={e => update(item, { check_interval_seconds: Number(e.target.value) })}>{intervals.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><label className="switch-label"><input type="checkbox" checked={item.notify_enabled} onChange={e => update(item, { notify_enabled: e.target.checked })} /><span>邮件提醒</span></label></div><p className="hint">最后检查：{formatTime(item.last_evaluated_at)} · {formatInterval(item.check_interval_seconds)}</p><p className={`next-check ${!item.notify_enabled ? "muted" : ""}`}>{!item.notify_enabled ? "自动监控未开启" : countdown === 0 ? "即将检查" : `距离下次自动检查还有 ${countdown ?? "—"} 秒`}</p>{historyExpanded && <HistoryChart points={historyState?.points || []} range={historyState?.range || "24h"} onRangeChange={range => changeHistoryRange(item, range)} loading={Boolean(historyState?.pending)} />}<div className="favorite-actions"><a className="button outline" href={item.product.detail_url} target="_blank" rel="noopener noreferrer">前往 B 站市集 ↗</a><button className="button outline" onClick={() => void refresh(item)} disabled={refreshingId === item.id}>{refreshingId === item.id ? "刷新中…" : "立即刷新"}</button><button className="button outline" onClick={() => toggleHistory(item)}>{historyExpanded ? "隐藏价格历史" : "价格历史"}</button><button className="button danger" onClick={() => void remove(item)}>删除</button></div></article>; })}</div>}</main>{monitorModal && <MonitorRequirementModal kind={monitorModal} onPrimary={() => { setMonitorModal(null); onOpenProfile(); }} onClose={() => setMonitorModal(null)} />}</>;
}

function EmailSuccessModal({ changing, onClose }: { changing: boolean; onClose: () => void }) {
  const modalRef = useRef<HTMLElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const previousFocus = document.activeElement as HTMLElement | null;
    closeButtonRef.current?.focus();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); onClose(); }
      if (event.key !== "Tab") return;
      const focusable = modalRef.current?.querySelectorAll<HTMLElement>("button, [href], input, select, textarea, [tabindex]:not([tabindex=\"-1\"]):not([disabled])");
      if (!focusable?.length) { event.preventDefault(); modalRef.current?.focus(); return; }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;
      if (!modalRef.current?.contains(active)) { event.preventDefault(); first.focus(); }
      else if (event.shiftKey && active === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && active === last) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      previousFocus?.focus();
    };
  }, [onClose]);
  return <div className="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="email-success-title" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}><section ref={modalRef} className="modal-card" tabIndex={-1}><span className="eyebrow">EMAIL STATUS</span><h2 id="email-success-title">{changing ? "通知邮箱已更换" : "邮箱验证成功"}</h2><p>{changing ? "新邮箱已验证并锁定，后续邮件提醒将发送到新邮箱。" : "通知邮箱已验证；开启自动监控还需获得管理员开通权限。"}</p><button ref={closeButtonRef} className="primary" onClick={onClose}>知道了</button></section></div>;
}

type MonitorAccessResponse = { status?: MonitorAccessStatus; monitor_access_status?: MonitorAccessStatus; request?: MonitorAccessRequest | null; monitor_access_request?: MonitorAccessRequest | null };

function normalizeMonitorAccess(data: MonitorAccessResponse, fallbackStatus: MonitorAccessStatus = "not_requested") {
  const status = data.status || data.monitor_access_status || fallbackStatus;
  const request = data.request === undefined ? data.monitor_access_request || null : data.request;
  return { status, request };
}

function monitorAccessLabel(status: MonitorAccessStatus | undefined) {
  return status === "approved" ? "已开通" : status === "pending" ? "审核中" : status === "rejected" ? "暂未通过" : "未开通";
}

function validMonitorReason(value: string) {
  const reason = value.trim();
  return reason.length >= 10 && reason.length <= 500 && !/<[^>]*>/.test(reason) && !/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/.test(reason);
}

function ProfilePage({ user, setUser }: { user: User; setUser: (user: User) => void }) {
  const [email, setEmail] = useState(user.email || "");
  const [code, setCode] = useState("");
  const [message, setMessage] = useState("");
  const [accessMessage, setAccessMessage] = useState("");
  const [accessStatus, setAccessStatus] = useState<MonitorAccessStatus>(user.monitor_access_status || "not_requested");
  const [accessRequest, setAccessRequest] = useState<MonitorAccessRequest | null>(null);
  const [accessLoading, setAccessLoading] = useState(true);
  const [accessBusy, setAccessBusy] = useState(false);
  const [reason, setReason] = useState("");
  const [cooldown, setCooldown] = useState(0);
  const [editing, setEditing] = useState(!user.email_verified);
  const [verificationStarted, setVerificationStarted] = useState(false);
  const [successModal, setSuccessModal] = useState<boolean | null>(null);
  const emailInputRef = useRef<HTMLInputElement>(null);
  const userRef = useRef(user);

  useEffect(() => { userRef.current = user; }, [user]);

  const refreshMonitorAccess = useCallback(async (isActive: () => boolean = () => true) => {
    const data = await api<MonitorAccessResponse>("/profile/monitor-access");
    const normalized = normalizeMonitorAccess(data, userRef.current.monitor_access_status || "not_requested");
    if (!isActive()) return null;
    setAccessStatus(normalized.status);
    setAccessRequest(normalized.request);
    setReason(normalized.request?.reason || "");
    setUser({ ...userRef.current, monitor_access_status: normalized.status });
    return normalized;
  }, [setUser]);

  useEffect(() => {
    if (!editing) setEmail(user.email || "");
  }, [editing, user.email]);
  useEffect(() => {
    let active = true;
    setAccessLoading(true);
    void refreshMonitorAccess(() => active).catch(error => {
      if (active) setAccessMessage(toUserMessage(error, "暂时无法获取监控功能权限状态，请稍后重试。"));
    }).finally(() => { if (active) setAccessLoading(false); });
    return () => { active = false; };
  }, [refreshMonitorAccess, user.id]);
  useEffect(() => {
    if (!editing) return;
    const frame = window.requestAnimationFrame(() => emailInputRef.current?.focus());
    return () => window.cancelAnimationFrame(frame);
  }, [editing]);
  useEffect(() => {
    if (!cooldown) return;
    const timer = window.setInterval(() => setCooldown(value => Math.max(0, value - 1)), 1000);
    return () => window.clearInterval(timer);
  }, [cooldown]);

  function clearVerificationForm() {
    setCode("");
    setCooldown(0);
    setMessage("");
    setVerificationStarted(false);
  }

  function startChange() {
    setEmail("");
    clearVerificationForm();
    setEditing(true);
  }

  function cancelChange() {
    setEmail(user.email || "");
    clearVerificationForm();
    setEditing(false);
  }

  async function sendCode() {
    setMessage("");
    try {
      await api("/profile/email/send-code", { method: "POST", body: JSON.stringify({ email: email.trim() }) });
      setCooldown(60);
      setVerificationStarted(true);
      setMessage("验证码已发送，请检查邮箱。");
    } catch (error) { setMessage(toUserMessage(error)); }
  }

  async function verify() {
    const changing = user.email_verified;
    try {
      const data = await api<{ email: string; email_verified: boolean }>("/profile/email/verify", { method: "POST", body: JSON.stringify({ code }) });
      setUser({ ...userRef.current, email: data.email, email_verified: data.email_verified });
      setEmail(data.email);
      clearVerificationForm();
      setEditing(false);
      setSuccessModal(changing);
      window.dispatchEvent(new Event("bmm:notifications-refresh"));
    } catch (error) { setMessage(toUserMessage(error)); }
  }

  async function submitMonitorAccess(event: FormEvent) {
    event.preventDefault();
    const trimmed = reason.trim();
    if (!user.email_verified) { setAccessMessage("请先绑定并验证通知邮箱，再提交监控功能申请。"); return; }
    if (!validMonitorReason(trimmed)) { setAccessMessage("申请理由需为纯文本，trim 后长度为 10～500 个字符，不能包含 HTML 标签或控制字符。"); return; }
    setAccessBusy(true);
    setAccessMessage("");
    try {
      const data = await api<MonitorAccessResponse>("/profile/monitor-access/request", { method: "POST", body: JSON.stringify({ reason: trimmed }) });
      const normalized = normalizeMonitorAccess(data, "pending");
      setAccessStatus(normalized.status);
      setAccessRequest(normalized.request || { reason: trimmed, submitted_at: new Date().toISOString() });
      setReason(normalized.request?.reason || trimmed);
      setUser({ ...userRef.current, monitor_access_status: normalized.status });
      setAccessMessage("申请已提交，请等待管理员审核。");
    } catch (error) {
      setAccessMessage(toUserMessage(error));
      if (error instanceof ApiRequestError && (error.code === "MONITOR_ACCESS_REQUEST_ALREADY_PENDING" || error.code === "MONITOR_ACCESS_ALREADY_APPROVED")) {
        const fallbackStatus = error.code === "MONITOR_ACCESS_REQUEST_ALREADY_PENDING" ? "pending" : "approved";
        setAccessStatus(fallbackStatus);
        setUser({ ...userRef.current, monitor_access_status: fallbackStatus });
        try { await refreshMonitorAccess(); } catch { /* retain the authoritative status from the conflict response */ }
      }
    } finally { setAccessBusy(false); }
  }

  const canEditReason = accessStatus === "not_requested" || accessStatus === "rejected";
  const accessDisabled = accessLoading || accessBusy || !user.email_verified || !canEditReason;

  return <main className="page narrow"><section className="section-heading"><span className="eyebrow">PROFILE</span><h1>个人资料</h1><p>开启自动监控需要先验证通知邮箱，并获得管理员开通权限。</p></section><section className="panel"><div className="profile-row"><span>用户名</span><strong>{user.username}</strong></div><div className="profile-row"><span>邮箱状态</span><strong className={user.email_verified ? "success-text" : "warning-text"}>{user.email_verified ? "已验证且有效" : "未验证"}</strong></div><label>通知邮箱<input ref={emailInputRef} type="email" value={editing ? email : user.email || ""} readOnly={!editing} onChange={event => setEmail(event.target.value)} placeholder="you@example.com" aria-describedby={user.email_verified && editing ? "email-change-hint" : undefined} /></label>{user.email_verified && !editing ? <div className="profile-actions"><p className="hint">当前邮箱已验证并正常接收提醒。</p><button type="button" className="button outline" onClick={startChange}>更换邮箱</button></div> : <div className="verification-area">{user.email_verified && <p id="email-change-hint" className="notice">当前已验证邮箱：{user.email}。验证新邮箱前，旧邮箱仍保持已验证并继续有效。</p>}<div className="button-row"><button type="button" className="primary" disabled={!email.trim() || cooldown > 0} onClick={() => void sendCode()}>{cooldown ? `${cooldown} 秒后可重发` : "发送验证码"}</button>{user.email_verified && <button type="button" className="button ghost" onClick={cancelChange}>取消更换</button>}</div>{verificationStarted && <div className="button-row"><input className="code-input" value={code} onChange={event => setCode(event.target.value.replace(/\D/g, "").slice(0, 6))} placeholder="6 位验证码" maxLength={6} inputMode="numeric" aria-label="邮箱验证码" /><button type="button" className="button outline" disabled={code.length !== 6} onClick={() => void verify()}>验证邮箱</button></div>}{message && <p className="notice" role="status" aria-live="polite">{message}</p>}</div>}</section><section className="panel monitor-access-panel"><div className="panel-title-row"><div><h2>监控功能权限</h2><p className="hint">状态：<strong className={accessStatus === "approved" ? "success-text" : accessStatus === "rejected" ? "warning-text" : ""}>{monitorAccessLabel(accessStatus)}</strong></p></div>{accessLoading && <span className="hint">正在加载…</span>}</div>{accessStatus === "approved" && <p className="notice">你可以正常开启商品自动监控和邮件提醒。</p>}{accessStatus === "not_requested" && !user.email_verified && <p className="notice warning">请先绑定并验证通知邮箱，再提交监控功能申请。</p>}{accessStatus === "not_requested" && user.email_verified && <p className="field-hint">请说明需要监控的商品和使用场景，理由需为 10～500 个纯文本字符。</p>}{accessStatus === "pending" && <><p className="notice">申请已提交，请等待管理员审核。</p>{accessRequest && <div className="access-request-summary"><p><strong>最近申请理由</strong></p><p>{accessRequest.reason}</p><span className="field-hint">提交时间：{formatTime(accessRequest.submitted_at)}</span></div>}</>}{accessStatus === "rejected" && <>{accessRequest?.review_note && <p className="notice warning"><strong>管理员说明：</strong>{accessRequest.review_note}</p>}<p className="field-hint">你可以修改申请理由后重新提交。</p></>} {canEditReason && <form className="monitor-access-form" onSubmit={submitMonitorAccess}><label>申请理由<textarea value={reason} onChange={event => setReason(event.target.value)} minLength={10} maxLength={500} rows={5} disabled={accessDisabled} aria-describedby="monitor-access-reason-hint" /></label><span id="monitor-access-reason-hint" className="field-hint">{reason.trim().length} / 500 字符 · 需要至少 10 个字符，按纯文本提交。</span><button className="primary" type="submit" disabled={accessDisabled}>{accessBusy ? "提交中…" : accessStatus === "rejected" ? "重新提交监控功能申请" : "提交监控功能申请"}</button></form>}{accessMessage && <p className="notice" role="status" aria-live="polite">{accessMessage}</p>}</section>{successModal !== null && <EmailSuccessModal changing={successModal} onClose={() => setSuccessModal(null)} />}</main>;
}

function TutorialPage({ onBack }: { onBack: () => void }) {
  return <main className="page tutorial-page"><section className="tutorial-intro"><span className="eyebrow">CLS ID GUIDE</span><h1>如何找到 B 站市集商品的 ClsId</h1><p>先在 BiliMarketBoss 搜索并收藏目标商品，再从收藏页复制 ClsId，回到 Bili Market Monitor 查询价格和设置提醒。</p></section><section className="tutorial-steps"><article className="tutorial-step"><div><span className="step-number">步骤 1</span><h2>进入搜索</h2><p>打开 BiliMarketBoss 首页，点击“开始搜索”。</p></div><img src="/tutorial/bili-market-boss-step-1.png" alt="步骤一：在 BiliMarketBoss 首页点击开始搜索" /></article><article className="tutorial-step"><div><span className="step-number">步骤 2～3</span><h2>输入关键词并开始搜索</h2><p>在“搜索关键词”文本框输入目标商品关键词，例如“初音未来”，然后点击页面下方的“开始搜索”。</p></div><img src="/tutorial/bili-market-boss-step-2-3.png" alt="步骤二、三：输入目标商品关键词并点击开始搜索" /></article><article className="tutorial-step"><div><span className="step-number">步骤 4～5</span><h2>收藏目标商品并进入收藏页</h2><p>在搜索结果中找到目标商品，点击商品卡片下方的心形收藏按钮。收藏后，点击顶栏“收藏”进入收藏页。</p></div><img src="/tutorial/bili-market-boss-step-4-5.png" alt="步骤四、五：点击目标商品收藏按钮并进入顶栏收藏页" /></article><article className="tutorial-step"><div><span className="step-number">步骤 6</span><h2>复制 ClsId</h2><p>在收藏页找到目标商品，复制商品信息中的 ClsId 纯数字，最后回到本站首页粘贴到“商品 ID”输入框中查询。</p></div><img src="/tutorial/bili-market-boss-step-6.png" alt="步骤六：在收藏页面复制目标商品的 ClsId" /></article></section><div className="tutorial-actions"><a className="button primary" href={BOSS_URL} target="_blank" rel="noopener noreferrer">打开 BiliMarketBoss ↗</a><button className="button outline" onClick={onBack}>返回查询商品</button></div></main>;
}

function ForgotPanel({ setMode }: { setMode: (mode: "login" | "register" | "forgot") => void }) { const [username, setUsername] = useState(""); const [email, setEmail] = useState(""); const [message, setMessage] = useState(""); async function submit(event: FormEvent) { event.preventDefault(); try { const data = await api<{ message: string }>("/auth/forgot-password", { method: "POST", body: JSON.stringify({ username, email }) }); setMessage(data.message); } catch (error) { setMessage(toUserMessage(error)); } } return <section className="auth-card"><div className="section-heading"><span className="eyebrow">ACCOUNT RECOVERY</span><h1>找回密码</h1><p>输入用户名和已验证邮箱，我们会发送重置链接。</p></div><form className="stack" onSubmit={submit}><label>用户名<input value={username} onChange={e => setUsername(e.target.value)} required /></label><label>绑定邮箱<input type="email" value={email} onChange={e => setEmail(e.target.value)} required /></label><button className="primary full">发送重置邮件</button></form>{message && <p className="notice">{message}</p>}<div className="auth-links"><button onClick={() => setMode("login")}>返回登录</button></div></section>; }

function ResetPasswordPage({ onDone }: { onDone: () => void }) {
  const token = new URLSearchParams(window.location.search).get("token") || "";
  const [password, setPassword] = useState(""); const [confirm, setConfirm] = useState(""); const [message, setMessage] = useState(""); const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent) { event.preventDefault(); if (!token) { setMessage("重置链接缺少 token，请重新申请。"); return; } if (password !== confirm) { setMessage("两次输入的密码不一致。"); return; } setBusy(true); setMessage(""); try { await api("/auth/reset-password", { method: "POST", body: JSON.stringify({ token, password }) }); setMessage("密码已重置，请使用新密码登录。"); window.setTimeout(onDone, 700); } catch (error) { setMessage(toUserMessage(error)); } finally { setBusy(false); } }
  return <section className="auth-card"><div className="section-heading"><span className="eyebrow">PASSWORD RESET</span><h1>设置新密码</h1><p>重置链接有效期有限，完成后需要重新登录。</p></div><form className="stack" onSubmit={submit}><label>新密码<input type="password" value={password} onChange={e => setPassword(e.target.value)} minLength={8} maxLength={128} required /></label><label>确认新密码<input type="password" value={confirm} onChange={e => setConfirm(e.target.value)} minLength={8} maxLength={128} required /></label>{message && <p className={message.includes("已重置") ? "notice" : "error"}>{message}</p>}<button className="primary full" disabled={busy}>{busy ? "提交中…" : "重置密码"}</button></form><div className="auth-links"><button onClick={onDone}>返回登录</button></div></section>;
}

type AdminUsageToday = { monitor_evaluations?: number; price_alerts?: number; notifications_created?: number; source?: string | null };
type AdminUser = User & { favorite_count?: number; enabled_monitor_count?: number; estimated_checks_per_day?: number; theoretical_checks_per_day?: number; usage_today?: AdminUsageToday; today_monitor_evaluations?: number; today_price_alerts?: number; today_notifications_created?: number };
type AdminFavorite = { id?: string; favorite_id?: string; cluster_id: number; title?: string; notify_enabled?: boolean; check_interval_seconds?: number; next_check_at?: string | null; last_evaluated_at?: string | null; target_price?: string | null; current_price?: string | null; available?: boolean; last_alert_price?: string | null; last_alert_at?: string | null };
type AdminMonitorAccessRequest = { id?: string; user_id: string; username: string; email?: string | null; email_verified?: boolean; monitor_access_status: MonitorAccessStatus; reason: string; submitted_at: string; reviewed_at?: string | null; review_note?: string | null; favorite_count?: number; enabled_monitor_count?: number };
type AdminUserDetail = AdminUser & { monitor_access_request?: MonitorAccessRequest | null; favorites?: AdminFavorite[]; monitoring?: { favorites?: AdminFavorite[]; favorite_count?: number; enabled_monitor_count?: number; estimated_checks_per_day?: number } };
type AdminTab = "overview" | "users" | "access_requests" | "monitoring" | "notifications" | "system";
type AdminRequestKey = "dashboard" | "users" | "access_requests" | "user-detail" | "monitoring" | "notifications" | "system";
type AdminNotificationRecord = { id: string | number; kind?: string | null; severity?: string | null; title?: string | null; body?: string | null; target_user_id?: string | null; created_at?: string | null; expires_at?: string | null; read_count?: number | null; created_by_admin_username?: string | null };
type AdminMonitoringItem = { cluster_id: number; title?: string | null; current_price?: string | null; available?: boolean; notify_subscribers?: number; enabled_monitors?: number; subscribers?: number; fastest_interval_seconds?: number | null; last_success_at?: string | null; last_error?: string | null; next_check_at?: string | null };

const adminTabs: Array<[AdminTab, string]> = [["overview", "概览"], ["users", "用户管理"], ["access_requests", "监控申请"], ["monitoring", "监控状态"], ["notifications", "通知管理"], ["system", "系统状态"]];
const notificationTemplates: Array<[string, string, string, string]> = [
  ["version_update", "版本更新", "Bili Market Monitor版本更新", "B站市集好价提示系统已完成版本更新。本次包含功能优化和稳定性改进，如遇异常可重新登录或稍后重试。"],
  ["load_advice", "监控负载建议", "关于监控任务的使用建议", "系统检测到你的监控商品数量较多或部分商品使用了较高刷新频率，可能产生较高的监控负载。为保证长期稳定使用，建议根据实际需要适当减少同时监控的商品，或将非紧急商品调整为更低的刷新频率。本消息仅为使用建议，不会自动修改你的任何设置。"],
  ["maintenance", "系统维护", "系统维护通知", "系统计划进行短时维护，维护期间商品检查或邮件提醒可能出现延迟。维护完成后服务会自动恢复。"],
  ["email_setup", "邮箱设置", "请检查你的通知邮箱设置", "为了正常接收低价邮件提醒，请确认已在“个人资料”中绑定并验证可用的通知邮箱。"],
  ["custom", "自定义", "", ""],
];

function maskedEmail(value: string | null | undefined) {
  if (!value) return "—";
  const [local, domain] = value.split("@");
  if (!domain) return value;
  if (local.includes("*")) return value;
  const visible = local.length <= 2 ? local.slice(0, 1) : local.slice(0, 2);
  return `${visible}${"*".repeat(Math.max(1, local.length - visible.length))}@${domain}`;
}

function estimatedChecksPerDay(item: AdminFavorite) {
  const seconds = Number(item.check_interval_seconds);
  return Number.isFinite(seconds) && seconds > 0 ? 86400 / seconds : 0;
}

function adminFavoriteId(item: AdminFavorite) {
  return item.id || item.favorite_id || null;
}

function AdminPanel() {
  const [activeTab, setActiveTab] = useState<AdminTab>("overview");
  const [dashboard, setDashboard] = useState<Record<string, any> | null>(null);
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [userQuery, setUserQuery] = useState("");
  const [appliedUserQuery, setAppliedUserQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [verificationFilter, setVerificationFilter] = useState("all");
  const [roleFilter, setRoleFilter] = useState("all");
  const [sortOrder, setSortOrder] = useState("created_at");
  const [userPage, setUserPage] = useState(1);
  const [userPageSize] = useState(20);
  const [userTotal, setUserTotal] = useState(0);
  const [selected, setSelected] = useState<AdminUserDetail | null>(null);
  const [accessRequests, setAccessRequests] = useState<AdminMonitorAccessRequest[]>([]);
  const [accessLoading, setAccessLoading] = useState(false);
  const [monitoring, setMonitoring] = useState<AdminMonitoringItem[]>([]);
  const [siteNotifications, setSiteNotifications] = useState<AdminNotificationRecord[]>([]);
  const [systemStatus, setSystemStatus] = useState<Record<string, any> | null>(null);
  const [message, setMessage] = useState("");
  const [loading, setLoading] = useState(false);
  const [targetMode, setTargetMode] = useState<"user" | "broadcast">("user");
  const [targetUserId, setTargetUserId] = useState("");
  const [sendEmail, setSendEmail] = useState(false);
  const [templateKind, setTemplateKind] = useState(notificationTemplates[0][0]);
  const [notificationSeverity, setNotificationSeverity] = useState("info");
  const [notificationTitle, setNotificationTitle] = useState(notificationTemplates[0][2]);
  const [notificationBody, setNotificationBody] = useState(notificationTemplates[0][3]);
  const [notificationActionUrl, setNotificationActionUrl] = useState("");
  const [notificationExpiresAt, setNotificationExpiresAt] = useState("");
  const [sendingNotification, setSendingNotification] = useState(false);
  const [selectedFavoriteIds, setSelectedFavoriteIds] = useState<string[]>([]);
  const [batchInterval, setBatchInterval] = useState(300);
  const [batchBusy, setBatchBusy] = useState(false);
  const mountedRef = useRef(true);
  const requestIdsRef = useRef<Record<AdminRequestKey, number>>({ dashboard: 0, users: 0, access_requests: 0, "user-detail": 0, monitoring: 0, notifications: 0, system: 0 });
  const controllersRef = useRef<Partial<Record<AdminRequestKey, AbortController>>>({});

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      (Object.keys(controllersRef.current) as AdminRequestKey[]).forEach(key => controllersRef.current[key]?.abort());
    };
  }, []);

  function beginAdminRequest(key: AdminRequestKey) {
    controllersRef.current[key]?.abort();
    const controller = new AbortController();
    const requestId = (requestIdsRef.current[key] || 0) + 1;
    requestIdsRef.current[key] = requestId;
    controllersRef.current[key] = controller;
    return { controller, requestId };
  }

  function isCurrentAdminRequest(key: AdminRequestKey, requestId: number, controller: AbortController) {
    return mountedRef.current && requestIdsRef.current[key] === requestId && !controller.signal.aborted;
  }

  const loadDashboard = useCallback(async () => {
    const request = beginAdminRequest("dashboard");
    try {
      const data = await api<Record<string, any>>("/admin/dashboard", { signal: request.controller.signal });
      if (isCurrentAdminRequest("dashboard", request.requestId, request.controller)) setDashboard(data);
    } catch (error) {
      if (isCurrentAdminRequest("dashboard", request.requestId, request.controller)) setMessage(toUserMessage(error));
    }
  }, []);

  const loadUsers = useCallback(async () => {
    const request = beginAdminRequest("users");
    setLoading(true);
    try {
      const query = new URLSearchParams({ page: String(userPage), page_size: String(userPageSize), q: appliedUserQuery, sort: sortOrder });
      if (statusFilter !== "all") query.set("status", statusFilter);
      if (verificationFilter !== "all") query.set("email_verified", verificationFilter === "verified" ? "true" : "false");
      if (roleFilter !== "all") query.set("role", roleFilter);
      const data = await api<{ items?: AdminUser[]; total?: number }>(`/admin/users?${query.toString()}`, { signal: request.controller.signal });
      if (!isCurrentAdminRequest("users", request.requestId, request.controller)) return;
      setUsers(data.items || []);
      setUserTotal(Number(data.total ?? data.items?.length ?? 0));
    } catch (error) {
      if (isCurrentAdminRequest("users", request.requestId, request.controller)) setMessage(toUserMessage(error));
    } finally {
      if (isCurrentAdminRequest("users", request.requestId, request.controller)) setLoading(false);
    }
  }, [appliedUserQuery, roleFilter, sortOrder, statusFilter, userPage, userPageSize, verificationFilter]);

  const loadAccessRequests = useCallback(async () => {
    const request = beginAdminRequest("access_requests");
    setAccessLoading(true);
    try {
      const data = await api<{ items?: AdminMonitorAccessRequest[] }>("/admin/monitor-access-requests", { signal: request.controller.signal });
      if (!isCurrentAdminRequest("access_requests", request.requestId, request.controller)) return;
      setAccessRequests(data.items || []);
    } catch (error) {
      if (isCurrentAdminRequest("access_requests", request.requestId, request.controller)) setMessage(toUserMessage(error));
    } finally {
      if (isCurrentAdminRequest("access_requests", request.requestId, request.controller)) setAccessLoading(false);
    }
  }, []);

  const loadMonitoring = useCallback(async () => {
    const request = beginAdminRequest("monitoring");
    try {
      const data = await api<{ items?: AdminMonitoringItem[] }>("/admin/bili-products", { signal: request.controller.signal });
      if (!isCurrentAdminRequest("monitoring", request.requestId, request.controller)) return;
      setMonitoring(data.items || []);
    } catch (error) {
      if (isCurrentAdminRequest("monitoring", request.requestId, request.controller)) setMessage(toUserMessage(error));
    }
  }, []);

  const loadSiteNotifications = useCallback(async () => {
    const request = beginAdminRequest("notifications");
    try {
      const data = await api<{ items?: AdminNotificationRecord[] }>("/admin/site-notifications", { signal: request.controller.signal });
      if (!isCurrentAdminRequest("notifications", request.requestId, request.controller)) return;
      setSiteNotifications(data.items || []);
    } catch (error) {
      if (isCurrentAdminRequest("notifications", request.requestId, request.controller)) setMessage(toUserMessage(error));
    }
  }, []);

  const loadSystemStatus = useCallback(async () => {
    const request = beginAdminRequest("system");
    try {
      const data = await api<Record<string, any>>("/admin/system-status", { signal: request.controller.signal });
      if (isCurrentAdminRequest("system", request.requestId, request.controller)) setSystemStatus(data);
    } catch (error) {
      if (isCurrentAdminRequest("system", request.requestId, request.controller)) setMessage(toUserMessage(error));
    }
  }, []);

  const loadUsersRef = useRef(loadUsers);
  const loadDashboardRef = useRef(loadDashboard);
  useEffect(() => { loadUsersRef.current = loadUsers; }, [loadUsers]);
  useEffect(() => { loadDashboardRef.current = loadDashboard; }, [loadDashboard]);

  useEffect(() => { void loadDashboard(); }, [loadDashboard]);
  useEffect(() => {
    if (activeTab === "users" || (activeTab === "notifications" && users.length === 0)) void loadUsers();
    if (activeTab === "access_requests") void loadAccessRequests();
    if (activeTab === "monitoring") void loadMonitoring();
    if (activeTab === "notifications") void loadSiteNotifications();
    if (activeTab === "system") void loadSystemStatus();
  }, [activeTab, loadAccessRequests, loadMonitoring, loadSiteNotifications, loadSystemStatus, loadUsers, users.length]);

  async function toggleUser(value: AdminUser) {
    try {
      await api(`/admin/users/${encodeURIComponent(value.id)}/${value.is_active ? "disable" : "enable"}`, { method: "POST" });
      await Promise.all([loadUsersRef.current(), loadDashboardRef.current()]);
    } catch (error) { if (mountedRef.current) setMessage(toUserMessage(error)); }
  }

  async function loadUserDetail(value: Pick<AdminUser, "id">) {
    const request = beginAdminRequest("user-detail");
    try {
      const data = await api<AdminUserDetail>(`/admin/users/${encodeURIComponent(value.id)}`, { signal: request.controller.signal });
      if (isCurrentAdminRequest("user-detail", request.requestId, request.controller)) setSelected(data);
    } catch (error) {
      if (isCurrentAdminRequest("user-detail", request.requestId, request.controller)) setMessage(toUserMessage(error));
    }
  }

  async function reviewAccessRequest(value: AdminMonitorAccessRequest, decision: "approve" | "reject") {
    let reviewNote = "";
    if (decision === "reject") {
      const entered = window.prompt("请输入驳回说明（可选，最多 500 个字符）：", value.review_note || "");
      if (entered === null) return;
      reviewNote = entered.trim().slice(0, 500);
    } else if (!window.confirm(`确认通过 ${value.username} 的监控功能申请吗？`)) return;
    try {
      await api(`/admin/monitor-access-requests/${encodeURIComponent(value.user_id)}/${decision}`, { method: "POST", body: decision === "reject" ? JSON.stringify({ review_note: reviewNote || null }) : undefined });
      setMessage(decision === "approve" ? "监控功能申请已通过。" : "监控功能申请已驳回。");
      await Promise.all([loadAccessRequests(), loadUsersRef.current(), loadDashboardRef.current()]);
      if (selected?.id === value.user_id) await loadUserDetail(selected);
    } catch (error) { if (mountedRef.current) setMessage(toUserMessage(error)); }
  }

  function applyTemplate(kind: string) {
    const template = notificationTemplates.find(value => value[0] === kind);
    if (!template) return;
    setTemplateKind(template[0]);
    setNotificationTitle(template[2]);
    setNotificationBody(template[3]);
  }

  const notificationTarget = targetMode === "user" ? (selected?.id === targetUserId ? selected : users.find(value => value.id === targetUserId)) : null;
  const smtpConfigured = Boolean(dashboard?.smtp?.configured);
  const notificationEmailAvailable = smtpConfigured && (targetMode === "broadcast" || Boolean(notificationTarget?.is_active && notificationTarget.email_verified));

  useEffect(() => {
    if (!notificationEmailAvailable) setSendEmail(false);
  }, [notificationEmailAvailable]);

  async function sendNotification(event: FormEvent) {
    event.preventDefault();
    if (targetMode === "user" && !targetUserId) { setMessage("请选择接收用户。"); return; }
    if (!notificationTitle.trim() || !notificationBody.trim()) { setMessage("请填写通知标题和正文。"); return; }
    if (sendEmail && !notificationEmailAvailable) { setMessage("当前没有可用的已验证邮箱或 SMTP 配置，只能发送站内通知。"); return; }
    if (targetMode === "broadcast" && !window.confirm(sendEmail ? "确认向所有用户发布站内广播，并向所有符合条件的已验证邮箱同步发送邮件吗？" : "确认仅向所有用户发布站内广播吗？")) return;
    setSendingNotification(true);
    setMessage("");
    try {
      const endpoint = targetMode === "user" ? `/admin/site-notifications/users/${encodeURIComponent(targetUserId)}` : "/admin/site-notifications/broadcast";
      const result = await api<{ email_queued_count?: number; email_skipped_count?: number; queued_count?: number; skipped_count?: number }>(endpoint, { method: "POST", body: JSON.stringify({ kind: "admin", template: templateKind, severity: notificationSeverity, title: notificationTitle.trim(), body: notificationBody.trim(), action_url: notificationActionUrl.trim() || null, expires_at: notificationExpiresAt ? new Date(notificationExpiresAt).toISOString() : null, send_email: sendEmail }) });
      if (!mountedRef.current) return;
      const queued = Number(result.email_queued_count ?? result.queued_count ?? (sendEmail && targetMode === "user" ? 1 : 0));
      const skipped = Number(result.email_skipped_count ?? result.skipped_count ?? 0);
      setMessage(!sendEmail ? (targetMode === "broadcast" ? "广播通知已发布。" : "定向通知已发送。") : targetMode === "broadcast" ? `广播已发布，已为${queued}个已验证邮箱加入邮件队列，${skipped}个用户未发送邮件。` : `站内通知已发送，${queued}封邮件已加入发送队列。`);
      await loadSiteNotifications();
    } catch (error) { if (mountedRef.current) setMessage(toUserMessage(error)); }
    finally { if (mountedRef.current) setSendingNotification(false); }
  }

  const statsUsers = dashboard?.users || {};
  const statsMonitoring = dashboard?.monitoring || {};
  const statsNotifications = dashboard?.notifications || {};
  const statsWorker = dashboard?.worker || {};
  const databaseStats = dashboard?.database || {};
  const rollupStats = databaseStats.price_history_rollup_rows || {};
  const compactionStats = statsWorker.metrics?.price_history_compaction || {};
  const configuredMinInterval = Number(dashboard?.global_min_interval_seconds);
  const globalMinInterval = configuredMinInterval > 0 ? configuredMinInterval : 10;
  const effectiveInterval = (seconds: number) => Math.max(Number(seconds) || globalMinInterval, globalMinInterval);
  const detailFavorites = selected?.favorites || selected?.monitoring?.favorites || [];
  const detailChecks = detailFavorites.reduce((total, item) => total + estimatedChecksPerDay(item), 0);
  const detailChecksEstimate = selected?.monitoring?.estimated_checks_per_day ?? selected?.theoretical_checks_per_day ?? detailChecks;
  const selectedDetailFavorites = detailFavorites.filter(item => {
    const id = adminFavoriteId(item);
    return id && selectedFavoriteIds.includes(id);
  });
  const selectedEnabledDetailFavorites = selectedDetailFavorites.filter(item => item.notify_enabled);
  const selectedChangedEnabledFavorites = selectedEnabledDetailFavorites.filter(item => Number(item.check_interval_seconds) !== batchInterval);
  const effectiveBatchInterval = effectiveInterval(batchInterval);
  const selectedBeforeChecks = selectedChangedEnabledFavorites.reduce((total, item) => total + 86400 / effectiveInterval(Number(item.check_interval_seconds)), 0);
  const selectedAfterChecks = selectedChangedEnabledFavorites.length ? selectedChangedEnabledFavorites.length * (86400 / effectiveBatchInterval) : 0;
  const hasAcceleration = selectedChangedEnabledFavorites.some(item => effectiveBatchInterval < effectiveInterval(Number(item.check_interval_seconds)));

  useEffect(() => {
    const defaultIds = detailFavorites.filter(item => item.notify_enabled).map(adminFavoriteId).filter((id): id is string => Boolean(id));
    setSelectedFavoriteIds(defaultIds);
  }, [selected]);

  function toggleFavoriteSelection(item: AdminFavorite, checked: boolean) {
    const id = adminFavoriteId(item);
    if (!id) return;
    setSelectedFavoriteIds(values => checked ? Array.from(new Set([...values, id])).slice(0, 20) : values.filter(value => value !== id));
  }

  async function batchUpdateInterval() {
    if (!selected) return;
    if (selectedFavoriteIds.length < 1 || selectedFavoriteIds.length > 20) { setMessage("请选择 1～20 件商品后再批量设置频率。"); return; }
    const before = selectedBeforeChecks.toFixed(1);
    const after = selectedAfterChecks.toFixed(1);
    const accelerationWarning = hasAcceleration ? "这会增加理论监控负载。" : "";
    if (!window.confirm(`确认将该用户选中的 ${selectedFavoriteIds.length} 件商品检查频率统一调整为 ${formatInterval(batchInterval)} 吗？\n调整前约 ${before} 次/日，调整后约 ${after} 次/日。${accelerationWarning}`)) return;
    setBatchBusy(true);
    setMessage("");
    try {
      const result = await api<{ updated_count?: number; no_op?: boolean; estimated_checks_per_day_before?: number; estimated_checks_per_day_after?: number }>(`/admin/users/${encodeURIComponent(selected.id)}/favorites/check-interval`, { method: "PATCH", body: JSON.stringify({ favorite_ids: selectedFavoriteIds, check_interval_seconds: batchInterval }) });
      const updatedCount = Number(result.updated_count ?? selectedFavoriteIds.length);
      setMessage(result.no_op || updatedCount === 0 ? "所选商品已经是该检查频率，没有产生变更。" : `已成功调整 ${updatedCount} 件商品的检查频率，并发送站内说明通知。`);
      await Promise.all([loadUserDetail(selected), loadUsersRef.current(), loadDashboardRef.current(), loadMonitoring()]);
    } catch (error) { if (mountedRef.current) setMessage(toUserMessage(error)); }
    finally { if (mountedRef.current) setBatchBusy(false); }
  }

  return <main className="page admin-page">
    <div className="page-heading"><div><span className="eyebrow">ADMIN</span><h1>管理后台</h1><p>版本 {dashboard?.version || "—"} · 全局最低请求间隔 {dashboard?.global_min_interval_seconds || "—"} 秒</p></div></div>
    {message && <p className="notice" role="status">{message}</p>}
    <div className="admin-tabs" role="tablist" aria-label="管理后台栏目">{adminTabs.map(([value, label]) => <button key={value} type="button" role="tab" aria-selected={activeTab === value} className={activeTab === value ? "active" : ""} onClick={() => setActiveTab(value)}>{label}</button>)}</div>
    {activeTab === "overview" && <>
      <div className="stats-grid"><div><span>用户</span><strong>{statsUsers.total ?? "—"}</strong><small>今日新增 {statsUsers.new_today ?? "—"}</small></div><div><span>已启用监控</span><strong>{statsMonitoring.enabled ?? "—"}</strong><small>商品 {statsMonitoring.unique_products ?? "—"} · 到期 {statsMonitoring.due_favorites || 0}</small></div><div><span>待发送邮件</span><strong>{statsNotifications.pending ?? "—"}</strong><small>失败 {statsNotifications.failed ?? "—"}</small></div><div><span>Worker 心跳</span><strong>{statsWorker.last_heartbeat ? "在线" : "离线"}</strong><small>{formatTime(statsWorker.last_heartbeat)} · 周期 {statsWorker.metrics?.last_cycle_duration_ms ?? "—"} ms</small></div><div><span>B站近24小时成功率</span><strong>{dashboard?.bili?.last_24h_success_rate == null ? "—" : `${dashboard.bili.last_24h_success_rate}%`}</strong><small>请求 {dashboard?.bili?.last_24h_requests ?? 0} · 平均 {dashboard?.bili?.last_24h_average_duration_ms ?? "—"} ms</small></div><div><span>SMTP</span><strong>{dashboard?.smtp?.configured ? "已配置" : "未配置"}</strong><small>失败 {dashboard?.smtp?.last_error || "—"}</small></div></div>
      <section className="panel"><h2>历史存储统计</h2><div className="stats-grid"><div><span>Raw历史记录</span><strong>{databaseStats.price_history_rows ?? "—"}</strong><small>原始观测行</small></div><div><span>1分钟聚合</span><strong>{rollupStats["60"] ?? "—"}</strong><small>Rollup行</small></div><div><span>5分钟聚合</span><strong>{rollupStats["300"] ?? "—"}</strong><small>Rollup行</small></div><div><span>30分钟聚合</span><strong>{rollupStats["1800"] ?? "—"}</strong><small>Rollup行</small></div></div></section>
      <section className="panel"><h2>最近一次历史压缩</h2><div className="stats-grid"><div><span>开始时间</span><strong>{compactionStats.started_at ? formatTime(compactionStats.started_at) : "暂无"}</strong><small>started_at</small></div><div><span>完成时间</span><strong>{compactionStats.finished_at ? formatTime(compactionStats.finished_at) : "暂无"}</strong><small>finished_at</small></div><div><span>生成行数</span><strong>{compactionStats.total_created ?? "暂无"}</strong><small>total_created</small></div><div><span>写入/合并次数</span><strong>{compactionStats.total_upserted ?? "暂无"}</strong><small>total_upserted</small></div><div><span>删除行数</span><strong>{compactionStats.total_deleted ?? "暂无"}</strong><small>total_deleted</small></div></div></section>
      <section className="panel"><h2>运行摘要</h2><p className="hint">失败 {dashboard?.bili?.last_24h_failures ?? 0} · 429 {dashboard?.bili?.last_24h_429 ?? 0} · 最近 Worker 心跳 {formatTime(statsWorker.last_heartbeat)}</p></section>
    </>}
    {activeTab === "users" && <section className="panel">
      <div className="panel-title-row"><h2>用户管理</h2><span className="hint">共 {userTotal} 个用户</span></div>
      <form className="admin-filter-row" onSubmit={event => { event.preventDefault(); setUserPage(1); setAppliedUserQuery(userQuery.trim()); }}><input value={userQuery} onChange={event => setUserQuery(event.target.value)} placeholder="搜索用户名或邮箱" aria-label="搜索用户" /><select value={statusFilter} onChange={event => { setUserPage(1); setStatusFilter(event.target.value); }}><option value="all">全部状态</option><option value="active">正常</option><option value="inactive">已禁用</option></select><select value={verificationFilter} onChange={event => { setUserPage(1); setVerificationFilter(event.target.value); }}><option value="all">全部邮箱状态</option><option value="verified">已验证</option><option value="unverified">未验证</option></select><select value={roleFilter} onChange={event => { setUserPage(1); setRoleFilter(event.target.value); }}><option value="all">全部角色</option><option value="user">普通用户</option><option value="admin">管理员</option></select><select value={sortOrder} onChange={event => { setUserPage(1); setSortOrder(event.target.value); }}><option value="created_at">注册时间 ↓</option><option value="username">用户名 ↓</option><option value="favorite_count">收藏数 ↓</option><option value="enabled_monitor_count">启用监控 ↓</option></select><button className="button outline" type="submit">搜索</button></form>
      {loading ? <p className="hint">正在加载用户…</p> : <div className="table-wrap"><table><thead><tr><th>用户名</th><th>邮箱（脱敏）</th><th>邮箱状态</th><th>监控权限</th><th>角色</th><th>账号状态</th><th>收藏</th><th>启用监控</th><th>估算检查/日</th><th>今日监控评估</th><th>今日低价触发</th><th>今日站内通知</th><th>操作</th></tr></thead><tbody>{users.map(value => <tr key={value.id}><td>{value.username}</td><td>{maskedEmail(value.email)}</td><td>{value.email_verified ? "已验证" : "未验证"}</td><td>{monitorAccessLabel(value.monitor_access_status)}</td><td>{value.role === "admin" ? "管理员" : "普通用户"}</td><td>{value.is_active ? "正常" : "已禁用"}</td><td>{value.favorite_count ?? "—"}</td><td>{value.enabled_monitor_count ?? "—"}</td><td>{value.estimated_checks_per_day ?? value.theoretical_checks_per_day ?? (value.enabled_monitor_count == null ? "—" : `约 ${Number(value.enabled_monitor_count) * 288}`)}</td><td>{value.usage_today?.monitor_evaluations ?? value.today_monitor_evaluations ?? "—"}</td><td>{value.usage_today?.price_alerts ?? value.today_price_alerts ?? "—"}</td><td>{value.usage_today?.notifications_created ?? value.today_notifications_created ?? "—"}</td><td><button className="table-button" onClick={() => void loadUserDetail(value)}>详情</button><button className="table-button" onClick={() => { setTargetMode("user"); setTargetUserId(value.id); setActiveTab("notifications"); }}>{"通知"}</button><button className="table-button" onClick={() => void toggleUser(value)}>{value.is_active ? "禁用" : "启用"}</button></td></tr>)}</tbody></table></div>}
      <div className="pagination"><button className="button outline" disabled={userPage <= 1 || loading} onClick={() => setUserPage(value => Math.max(1, value - 1))}>上一页</button><span>第 {userPage} 页 · 每页 {userPageSize} 条</span><button className="button outline" disabled={loading || userPage * userPageSize >= userTotal} onClick={() => setUserPage(value => value + 1)}>下一页</button></div>
      <p className="hint">检查次数为理论值估算，不包含接口限流、失败重试、维护策略等实际影响。</p><p className="hint">今日三项数据为应用层日统计（监控评估、低价触发、站内通知），不等同于 Azure/CDN 计费流量。</p>
    </section>}
    {activeTab === "access_requests" && <section className="panel"><div className="panel-title-row"><h2>监控功能申请</h2><button className="button outline" onClick={() => void loadAccessRequests()}>刷新申请</button></div>{accessLoading ? <p className="hint">正在加载申请…</p> : accessRequests.length === 0 ? <p className="hint">暂无待处理或历史申请。</p> : <div className="table-wrap"><table><thead><tr><th>用户</th><th>邮箱</th><th>邮箱状态</th><th>申请状态</th><th>申请理由</th><th>提交时间</th><th>收藏/启用监控</th><th>操作</th></tr></thead><tbody>{accessRequests.map(value => <tr key={value.user_id}><td>{value.username}</td><td>{maskedEmail(value.email)}</td><td>{value.email_verified ? "已验证" : "未验证"}</td><td>{monitorAccessLabel(value.monitor_access_status)}</td><td className="admin-reason-cell">{value.reason}</td><td>{formatTime(value.submitted_at)}</td><td>{value.favorite_count ?? "—"} / {value.enabled_monitor_count ?? "—"}</td><td>{value.monitor_access_status === "pending" ? <><button className="table-button" onClick={() => void reviewAccessRequest(value, "approve")}>通过</button><button className="table-button" onClick={() => void reviewAccessRequest(value, "reject")}>驳回</button></> : <span className="table-subtext">已处理</span>}<button className="table-button" onClick={() => void loadUserDetail({ id: value.user_id })}>详情</button></td></tr>)}</tbody></table></div>}</section>}
      {activeTab === "monitoring" && <section className="panel"><div className="panel-title-row"><h2>监控状态</h2><button className="button outline" onClick={() => void loadMonitoring()}>刷新</button></div><div className="table-wrap"><table><thead><tr><th>商品</th><th>当前价格</th><th>订阅/启用监控</th><th>下次检查</th><th>最近成功</th><th>错误</th></tr></thead><tbody>{monitoring.map(item => <tr key={item.cluster_id}><td>{item.title || item.cluster_id}<small className="table-subtext">{item.cluster_id}</small></td><td>{item.current_price ? `¥${item.current_price}` : "—"}</td><td>{item.subscribers ?? item.notify_subscribers ?? "—"} / {item.enabled_monitors ?? "—"}</td><td>{formatTime(item.next_check_at)}</td><td>{formatTime(item.last_success_at)}</td><td>{item.last_error || "—"}</td></tr>)}</tbody></table></div>{monitoring.length === 0 && <p className="hint">暂无监控商品数据。</p>}</section>}
      {activeTab === "notifications" && <section className="panel admin-notification-panel"><div className="panel-title-row"><h2>通知管理</h2><button className="button outline" onClick={() => void loadSiteNotifications()}>刷新记录</button></div><form className="stack" onSubmit={sendNotification}><div className="admin-form-grid"><label>发送范围<select value={targetMode} onChange={event => setTargetMode(event.target.value as "user" | "broadcast")}><option value="user">定向通知</option><option value="broadcast">全体广播</option></select></label>{targetMode === "user" && <label>接收用户<select value={targetUserId} onChange={event => setTargetUserId(event.target.value)}><option value="">请选择用户</option>{users.map(value => <option key={value.id} value={value.id}>{value.username} · {maskedEmail(value.email)}</option>)}</select></label>}<label>模板<select value={templateKind} onChange={event => applyTemplate(event.target.value)}>{notificationTemplates.map(value => <option key={value[0]} value={value[0]}>{value[1]}</option>)}</select></label><label>级别<select value={notificationSeverity} onChange={event => setNotificationSeverity(event.target.value)}><option value="info">提示</option><option value="success">成功</option><option value="warning">警告</option><option value="important">重要</option></select></label><label>标题<input value={notificationTitle} onChange={event => setNotificationTitle(event.target.value)} maxLength={120} readOnly={templateKind !== "custom"} aria-readonly={templateKind !== "custom"} required /></label><label>站内跳转（可选）<input value={notificationActionUrl} onChange={event => setNotificationActionUrl(event.target.value)} placeholder="/favorites" /></label></div><label className="switch-label admin-email-option"><input type="checkbox" checked={sendEmail} onChange={event => setSendEmail(event.target.checked)} disabled={!notificationEmailAvailable} /><span>同时发送到用户绑定邮箱</span></label>{targetMode === "user" && !notificationTarget?.email_verified && <p className="field-hint">该用户尚未验证邮箱，只能发送站内通知。</p>}{!smtpConfigured && <p className="field-hint">SMTP 未配置，只能发送站内通知。</p>}{targetMode === "broadcast" && smtpConfigured && <p className="field-hint">邮件只会发送到有效账号中的已验证邮箱。</p>}<p className="field-hint">预设模板使用规范文案；选择“自定义”后可编辑标题和正文。</p><label>正文<textarea value={notificationBody} onChange={event => setNotificationBody(event.target.value)} rows={4} maxLength={2000} readOnly={templateKind !== "custom"} aria-readonly={templateKind !== "custom"} required /></label><label>过期时间（可选）<input type="datetime-local" value={notificationExpiresAt} onChange={event => setNotificationExpiresAt(event.target.value)} /></label><button className="primary" disabled={sendingNotification}>{sendingNotification ? "提交中…" : targetMode === "broadcast" ? "发送广播" : "发送定向通知"}</button></form><div className="table-wrap"><table><thead><tr><th>时间</th><th>管理员</th><th>类型</th><th>标题</th><th>目标</th><th>已读</th><th>过期</th></tr></thead><tbody>{siteNotifications.map(item => <tr key={item.id}><td>{formatTime(item.created_at)}</td><td>{item.created_by_admin_username || "—"}</td><td>{item.kind || "—"}</td><td>{item.title || "—"}</td><td>{item.target_user_id || "全体"}</td><td>{item.read_count ?? "—"}</td><td>{formatTime(item.expires_at)}</td></tr>)}</tbody></table></div></section>}
    {activeTab === "system" && <section className="panel"><div className="panel-title-row"><h2>系统状态</h2><button className="button outline" onClick={() => void loadSystemStatus()}>刷新</button></div>{systemStatus ? <div className="system-status-grid">{Object.entries(systemStatus).map(([key, value]) => <div key={key}><span>{key}</span><strong>{typeof value === "object" ? JSON.stringify(value) : String(value ?? "—")}</strong></div>)}</div> : <p className="hint">正在加载系统状态…</p>}</section>}
    {selected && <section className="panel detail-panel">
      <div className="panel-title-row"><h2>用户详情：{selected.username}</h2><div className="button-row"><button className="button outline" onClick={() => { setTargetMode("user"); setTargetUserId(selected.id); setActiveTab("notifications"); }}>发送通知</button><button className="ghost" onClick={() => setSelected(null)}>关闭</button></div></div>
      <p className="hint">邮箱：{maskedEmail(selected.email)} · {selected.email_verified ? "已验证且有效" : "未验证"} · {selected.is_active ? "账号正常" : "账号已禁用"}</p>
      <p className="hint">监控权限：{monitorAccessLabel(selected.monitor_access_status)} · 收藏 {selected.favorite_count ?? selected.monitoring?.favorite_count ?? detailFavorites.length} 件 · 启用监控 {selected.enabled_monitor_count ?? selected.monitoring?.enabled_monitor_count ?? detailFavorites.filter(item => item.notify_enabled).length} 件 · 理论检查约 {Math.round(detailChecksEstimate)} 次/日（估算）</p>
      <p className="hint">今日监控评估 {selected.usage_today?.monitor_evaluations ?? selected.today_monitor_evaluations ?? "—"} · 今日低价触发 {selected.usage_today?.price_alerts ?? selected.today_price_alerts ?? "—"} · 今日站内通知 {selected.usage_today?.notifications_created ?? selected.today_notifications_created ?? "—"}</p>
      <p className="hint">今日数据为应用层日统计，不等同于 Azure/CDN 计费流量{selected.usage_today?.source ? `（${selected.usage_today.source}）` : "。"}</p>
      {selected.monitor_access_request && <div className="access-request-summary"><p><strong>最近监控申请</strong></p><p>{selected.monitor_access_request.reason}</p><span className="field-hint">提交时间：{formatTime(selected.monitor_access_request.submitted_at)}{selected.monitor_access_request.review_note ? ` · 审核说明：${selected.monitor_access_request.review_note}` : ""}</span></div>}
      <section className="detail-batch-panel">
        <div className="panel-title-row"><h3>批量调整检查频率</h3><span className="hint">最多选择 20 件</span></div>
        <div className="detail-batch-toolbar">
          <label className="switch-label"><input type="checkbox" checked={detailFavorites.filter(item => item.notify_enabled).every(item => { const id = adminFavoriteId(item); return id ? selectedFavoriteIds.includes(id) : true; }) && detailFavorites.some(item => item.notify_enabled)} onChange={event => setSelectedFavoriteIds(event.target.checked ? detailFavorites.filter(item => item.notify_enabled).map(adminFavoriteId).filter((id): id is string => Boolean(id)).slice(0, 20) : [])} /><span>全选已开启监控</span></label>
          <label>新检查频率<select value={batchInterval} onChange={event => setBatchInterval(Number(event.target.value))}>{intervals.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
          <button className="button primary" type="button" disabled={batchBusy || selectedFavoriteIds.length === 0} onClick={() => void batchUpdateInterval()}>{batchBusy ? "调整中…" : "批量设置频率"}</button>
        </div>
        <p className="field-hint">已选 {selectedFavoriteIds.length} 件 · 调整前约 {selectedFavoriteIds.length ? selectedBeforeChecks.toFixed(1) : "0.0"} 次/日 · 调整后约 {selectedFavoriteIds.length ? selectedAfterChecks.toFixed(1) : "0.0"} 次/日{hasAcceleration ? " · 这会增加理论监控负载" : ""}</p>
        <div className="table-wrap"><table className="detail-favorites-table"><thead><tr><th>选择</th><th>商品</th><th>自动监控</th><th>目标价</th><th>当前频率</th><th>下次检查</th><th>最后检查</th></tr></thead><tbody>{detailFavorites.length ? detailFavorites.map(item => { const id = adminFavoriteId(item); return <tr key={item.cluster_id}><td><input type="checkbox" aria-label={`选择${item.title || item.cluster_id}`} checked={Boolean(id && selectedFavoriteIds.includes(id))} disabled={!id || batchBusy} onChange={event => toggleFavoriteSelection(item, event.target.checked)} /></td><td>{item.title || "未命名商品"}<small className="table-subtext">{item.cluster_id}</small></td><td>{item.notify_enabled ? "已开启" : "已关闭"}</td><td>{item.target_price ? `¥${item.target_price}` : "未设置"}</td><td>{item.check_interval_seconds ? formatInterval(item.check_interval_seconds) : "频率未知"}</td><td>{formatTime(item.next_check_at)}</td><td>{formatTime(item.last_evaluated_at)}</td></tr>; }) : <tr><td colSpan={7}>暂无收藏监控信息。</td></tr>}</tbody></table></div>
      </section>
    </section>}
  </main>;
}

function pageFromPath(pathname: string) {
  if (["/reset-password", "/tutorial", "/favorites", "/profile", "/admin", "/login", "/forgot"].includes(pathname)) return pathname.slice(1);
  return "home";
}

function pathForPage(page: string) {
  if (["tutorial", "reset-password", "favorites", "profile", "admin", "login", "forgot"].includes(page)) return `/${page}`;
  return "/";
}

export default function App() {
  const [user, setUser] = useState<User | null>(null); const [page, setPageState] = useState(pageFromPath(window.location.pathname)); const [authMode, setAuthMode] = useState<"login" | "register" | "forgot">("login"); const [notificationClusterId, setNotificationClusterId] = useState<string | null>(null);
  useEffect(() => {
    void api<{ user: User }>("/auth/me").then(data => {
      setUser(data.user);
      if (window.location.pathname === "/admin" && data.user.role !== "admin") navigate("home");
    }).catch(() => {
      if (["/favorites", "/profile", "/admin"].includes(window.location.pathname)) navigate("login");
    });
  }, []);
  useEffect(() => { const onPopState = () => setPageState(pageFromPath(window.location.pathname)); window.addEventListener("popstate", onPopState); return () => window.removeEventListener("popstate", onPopState); }, []);
  function navigate(nextPage: string) { const nextPath = pathForPage(nextPage); if (window.location.pathname !== nextPath) window.history.pushState({}, "", nextPath); setPageState(nextPage); }
  function navigateNotification(nextPage: string, actionUrl?: string) {
    const match = actionUrl ? actionUrl.match(/^\/bili-market\/products\/(\d+)(?:[/?#]|$)/) : null;
    setNotificationClusterId(match?.[1] || null);
    navigate(nextPage);
  }
  async function logout() { await api("/auth/logout", { method: "POST" }); setUser(null); navigate("home"); }
  function login() { setAuthMode("login"); navigate("login"); }
  return <><Header user={user} page={page} setPage={navigate} onLogout={() => void logout()} onNotificationNavigate={navigateNotification} />{page === "home" && <SearchPage user={user} onRequireLogin={login} onOpenProfile={() => navigate("profile")} onOpenTutorial={() => navigate("tutorial")} initialClusterId={notificationClusterId} />}{page === "tutorial" && <TutorialPage onBack={() => navigate("home")} />}{page === "login" && <main className="page auth-page"><AuthPanel mode={authMode === "register" ? "register" : "login"} onSuccess={value => { setUser(value); navigate("home"); }} setMode={mode => { setAuthMode(mode); if (mode === "forgot") navigate("forgot"); }} /></main>}{page === "forgot" && <main className="page auth-page"><ForgotPanel setMode={mode => { setAuthMode(mode); navigate(mode === "forgot" ? "forgot" : "login"); }} /></main>}{page === "reset-password" && <main className="page auth-page"><ResetPasswordPage onDone={() => { navigate("login"); setAuthMode("login"); }} /></main>}{page === "favorites" && user && <FavoritesPage user={user} onProductRefresh={() => undefined} onOpenProfile={() => navigate("profile")} />}{page === "profile" && user && <ProfilePage user={user} setUser={setUser} />}{page === "admin" && user?.role === "admin" && <AdminPanel />}<footer><span>B站市集好价提示系统 · Bili Market Monitor · v{APP_VERSION}</span><a href={BOSS_URL} target="_blank" rel="noopener noreferrer">BiliMarketBoss 商品检索入口</a></footer></>;
}
