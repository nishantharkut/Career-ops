import { useCallback, useEffect, useRef, useState } from "react";

import { ApplicationPacket, getApplicationPacket } from "../api";

function message(error: unknown): string {
  return error instanceof Error ? error.message : "Application packet could not be loaded.";
}

export function useApplicationPacket(applicationId: string | null) {
  const [loadedPacket, setLoadedPacket] = useState<{
    applicationId: string;
    packet: ApplicationPacket;
  } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const controller = useRef<AbortController | null>(null);
  const generation = useRef(0);
  const currentApplicationId = useRef(applicationId);
  currentApplicationId.current = applicationId;

  const load = useCallback(async (targetId: string | null) => {
    if (targetId !== currentApplicationId.current) return null;
    controller.current?.abort();
    const requestGeneration = ++generation.current;
    if (!targetId) {
      setLoadedPacket(null);
      setError("");
      setLoading(false);
      return null;
    }

    const nextController = new AbortController();
    controller.current = nextController;
    setLoading(true);
    setError("");
    try {
      const nextPacket = await getApplicationPacket(targetId, nextController.signal);
      if (
        requestGeneration === generation.current &&
        targetId === currentApplicationId.current &&
        !nextController.signal.aborted
      ) {
        setLoadedPacket({ applicationId: targetId, packet: nextPacket });
        return nextPacket;
      }
    } catch (loadError) {
      if (
        requestGeneration === generation.current &&
        targetId === currentApplicationId.current &&
        !nextController.signal.aborted
      ) {
        setLoadedPacket(null);
        setError(message(loadError));
      }
    } finally {
      if (
        requestGeneration === generation.current &&
        targetId === currentApplicationId.current &&
        !nextController.signal.aborted
      ) {
        setLoading(false);
      }
    }
    return null;
  }, []);

  useEffect(() => {
    void load(applicationId);
    return () => controller.current?.abort();
  }, [applicationId, load]);

  const reload = useCallback(() => load(applicationId), [applicationId, load]);
  const packet =
    loadedPacket?.applicationId === applicationId ? loadedPacket.packet : null;

  return { packet, loading, error, reload };
}
