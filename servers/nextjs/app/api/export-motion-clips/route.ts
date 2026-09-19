import { NextRequest, NextResponse } from "next/server";
import { getFastApiAuthHeaders, getFastApiBaseUrl } from "@/lib/fastapi-internal";
import { authStatusForRequest } from "@/lib/server-auth-role";

/**
 * PPTX companion export: the .pptx keeps static images, so the presentation's
 * AI motion clips are bundled into a separate zip. Responds with
 * `{ count, path }` where `path` is a /api/export-presentation/file download
 * URL (count 0 / no path when the deck has no clips).
 */
export async function POST(req: NextRequest) {
  const auth = await authStatusForRequest(req);
  if (!auth.authenticated) {
    return NextResponse.json({ detail: "Unauthorized" }, { status: 401 });
  }

  let id: unknown;
  try {
    id = (await req.json())?.id;
  } catch {
    return NextResponse.json({ error: "Invalid request JSON body" }, { status: 400 });
  }
  if (typeof id !== "string" || !id.trim()) {
    return NextResponse.json({ error: "Missing Presentation ID" }, { status: 400 });
  }

  const cookie = req.headers.get("cookie") || "";
  try {
    const response = await fetch(
      `${getFastApiBaseUrl()}/api/v1/ppt/motion-video/presentation/${encodeURIComponent(
        id.trim()
      )}/export-clips`,
      {
        method: "POST",
        headers: {
          ...(cookie ? { cookie } : {}),
          ...getFastApiAuthHeaders(),
        },
        cache: "no-store",
      }
    );
    if (!response.ok) {
      return new NextResponse(await response.text(), {
        status: response.status,
        headers: {
          "content-type": response.headers.get("content-type") || "application/json",
        },
      });
    }
    const result = (await response.json()) as {
      count: number;
      relative_path?: string | null;
    };
    return NextResponse.json({
      count: result.count,
      path: result.relative_path
        ? `/api/export-presentation/file?name=${encodeURIComponent(result.relative_path)}`
        : null,
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    console.error("[export-motion-clips]", message);
    return NextResponse.json({ error: message, success: false }, { status: 500 });
  }
}
