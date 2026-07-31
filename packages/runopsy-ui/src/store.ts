/** What the user has selected. Nothing derived from the server lives here. */

import { create } from "zustand";

type Selection = {
  runId: string | null;
  focusedNodeId: string | null;
  selectRun: (runId: string) => void;
  focusNode: (nodeId: string | null) => void;
};

export const useSelection = create<Selection>((set) => ({
  runId: null,
  focusedNodeId: null,
  // Changing run clears the focused step: keeping it would leave the evidence panel
  // describing a step from a different trace, which reads as a bug in the diagnosis.
  selectRun: (runId) => set({ runId, focusedNodeId: null }),
  focusNode: (focusedNodeId) => set({ focusedNodeId }),
}));
