import { appendFileSync, mkdirSync } from "node:fs";
import path from "node:path";

type LogContext = Record<string, unknown>;

function write(level: "info" | "warn" | "error", message: string, context?: LogContext) {
  const entry = {
    timestamp: new Date().toISOString(),
    service: "project-dispatch",
    level,
    message,
    ...(context ? { context } : {}),
  };
  if ((process.env.NODE_ENV === "production" || process.env.NODE_ENV === "test") && level !== "info") {
    const output = JSON.stringify(entry);
    if (level === "error") console.error(output);
    else console.warn(output);
  }
  const logFile = process.env.DISPATCH_LOG_FILE || (process.env.VERCEL ? "/tmp/dispatch.log" : path.join(process.cwd(), "logs", "dispatch.log"));
  mkdirSync(path.dirname(logFile), { recursive: true });
  appendFileSync(logFile, `${JSON.stringify(entry)}\n`, "utf8");
}

export const log = {
  info: (message: string, context?: LogContext) => write("info", message, context),
  warn: (message: string, context?: LogContext) => write("warn", message, context),
  error: (message: string, context?: LogContext) => write("error", message, context),
};

export function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}
