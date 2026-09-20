/**
 * Upload intent — "somebody outside the Transactions page just asked for the
 * upload zone".
 *
 * Why this exists: a URL param alone cannot carry the request. `/transactions
 * ?upload=1` is read in a mount-only effect, which runs exactly once — on a
 * hard load. Asked for while already inside the app, the page is already
 * mounted (or the param was stripped by the previous request), so the effect
 * never runs again and the control does nothing. Pushing `?upload=1` from the
 * Transactions page would also throw away whatever is in the URL: the selected
 * statement tab, the filters, the page number.
 *
 * So the request is state, not a URL: the sidebar sets it, the Transactions
 * page consumes it. `?upload=1` still works as a deep link (the onboarding
 * completion screen and the reconciliation review both send users that way
 * from another route), it is just not the only channel.
 *
 * `pending` is consumed — set back to null — by whoever handles it, so
 * arriving on Transactions later by ordinary navigation does not re-open a zone
 * nobody asked for.
 */
import { create } from "zustand";

/** Which promise the zone is making. "auto" sniffs each PDF and routes a bank
 *  statement to the statement pipeline; "receipt" takes the user's word for it.
 *  See the note on `uploadMode` in `pages/Transactions.tsx`. */
export type UploadIntentMode = "auto" | "receipt";

interface UploadIntentState {
  pending: UploadIntentMode | null;
  /** Ask the Transactions page to open its upload zone. Safe to call from any
   *  page and safe to call when the zone is already open — the page re-opens
   *  it and scrolls it into view, so a second click is never a no-op. */
  requestUpload: (mode?: UploadIntentMode) => void;
  /** Called by the page once it has acted on the request. */
  consumeUpload: () => void;
}

export const useUploadIntentStore = create<UploadIntentState>((set) => ({
  pending: null,
  requestUpload: (mode = "auto") => set({ pending: mode }),
  consumeUpload: () => set({ pending: null }),
}));
