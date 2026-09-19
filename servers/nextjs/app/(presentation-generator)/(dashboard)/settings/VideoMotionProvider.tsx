"use client";

import React, { useCallback, useEffect } from "react";
import { Check } from "lucide-react";
import { Switch } from "@/components/ui/switch";
import { cn } from "@/lib/utils";

import { LLMConfig } from "@/types/llm_config";
import { MOTION_VIDEO_PROVIDERS } from "@/utils/providerConstants";
import { MixpanelEvent, trackEvent } from "@/utils/mixpanel";

const VideoMotionProvider = ({
  llmConfig,
  setLlmConfig,
}: {
  llmConfig: LLMConfig;
  setLlmConfig: React.Dispatch<React.SetStateAction<LLMConfig>>;
}) => {
  const isMotionDisabled = llmConfig.DISABLE_MOTION_VIDEO ?? false;
  const selectedProviderKey = llmConfig.MOTION_VIDEO_PROVIDER || "";

  const update = useCallback(
    (field: keyof LLMConfig, value: string | boolean) => {
      setLlmConfig((current) => ({ ...current, [field]: value }));
    },
    [setLlmConfig]
  );

  // ComfyUI is currently the only motion provider -- default it selected once
  // motion video is turned on, so the setup fields show immediately instead
  // of requiring an extra click on a one-item grid.
  useEffect(() => {
    if (!isMotionDisabled && !selectedProviderKey) {
      update("MOTION_VIDEO_PROVIDER", "comfyui");
    }
  }, [isMotionDisabled, selectedProviderKey, update]);

  const handleToggle = (checked: boolean) => {
    trackEvent(MixpanelEvent.Settings_Provider_Selected, {
      section: "motion_video",
      enabled: checked,
      provider: checked ? selectedProviderKey || "comfyui" : "disabled",
    });
    update("DISABLE_MOTION_VIDEO", !checked);
  };

  const handleSelectProvider = (providerKey: string) => {
    trackEvent(MixpanelEvent.Settings_Provider_Selected, {
      section: "motion_video",
      provider: providerKey,
    });
    update("MOTION_VIDEO_PROVIDER", providerKey);
  };

  const selectedProvider = MOTION_VIDEO_PROVIDERS[selectedProviderKey];

  return (
    <div className="space-y-6 rounded-[12px] bg-[#F9F8F8] p-7">
      <div className="mb-4 rounded-[12px] bg-white p-10 pt-5">
        <div className="flex items-center justify-end">
          <Switch
            checked={!isMotionDisabled}
            className="data-[state=checked]:bg-[#4791FF] data-[state=unchecked]:bg-gray-400"
            onCheckedChange={handleToggle}
          />
        </div>

        <div className="max-w-[290px] pb-[50px]">
          <div
            className="flex h-[60px] w-[60px] items-center justify-center rounded-[4px] px-[13.5px] py-[14.2px]"
            style={{ backgroundColor: "#F4F3FF" }}
          >
            <img
              src="/providers/comfyui-color.svg"
              className="h-full w-full object-cover"
              alt="motion-video"
            />
          </div>
          <h3 className="py-2.5 text-xl font-normal text-[#191919]">
            Motion Video Settings
          </h3>
          <p className="text-sm text-gray-500">
            Choosing how slide images become short motion clips
          </p>
        </div>

        {!isMotionDisabled && (
          <>
            <label className="mb-2 block text-sm font-medium text-gray-700">
              Select Motion Provider
            </label>
            <div className="grid w-full grid-cols-2 gap-3 sm:grid-cols-3">
              {Object.values(MOTION_VIDEO_PROVIDERS).map((provider) => {
                const isSelected = selectedProviderKey === provider.value;
                return (
                  <button
                    key={provider.value}
                    type="button"
                    onClick={() => handleSelectProvider(provider.value)}
                    className={cn(
                      "relative flex flex-col items-center gap-2 rounded-xl border p-4 transition-colors",
                      isSelected
                        ? "border-[#5146E5] bg-[#F4F3FF]"
                        : "border-gray-200 bg-white hover:border-gray-300"
                    )}
                  >
                    {isSelected && (
                      <Check className="absolute right-2 top-2 h-4 w-4 text-[#5146E5]" />
                    )}
                    <div className="flex h-10 w-10 items-center justify-center rounded-md bg-white">
                      {provider.icon ? (
                        <img
                          src={provider.icon}
                          alt={provider.label}
                          className="h-6 w-6 object-contain"
                        />
                      ) : null}
                    </div>
                    <span className="text-sm font-medium text-gray-900">
                      {provider.label}
                    </span>
                  </button>
                );
              })}
            </div>

            {selectedProvider?.value === "comfyui" && (
              <div className="mt-6 rounded-[12px] bg-[#F9F8F8] p-6">
                <h4 className="text-base font-medium text-[#191919]">
                  ComfyUI setup
                </h4>
                <p className="mb-4 text-sm text-gray-500">
                  Configure the selected motion provider before continuing.
                </p>

                <div className="space-y-4">
                  <div>
                    <label className="mb-2 block text-sm font-medium text-gray-700">
                      ComfyUI Server URL
                    </label>
                    <input
                      type="text"
                      placeholder="Defaults to your Image Provider's ComfyUI URL if left blank"
                      className="h-12 w-full rounded-lg border border-gray-300 px-4 text-sm text-[#191919] outline-none transition-colors focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20"
                      value={llmConfig.COMFYUI_MOTION_URL || ""}
                      onChange={(event) =>
                        update("COMFYUI_MOTION_URL", event.target.value)
                      }
                    />
                    <p className="mt-2 flex items-center gap-2 text-sm text-gray-500">
                      <span className="block h-1 w-1 rounded-full bg-gray-400"></span>
                      Use your machine IP address (not localhost) when
                      running in Docker
                    </p>
                  </div>

                  <div>
                    <label className="mb-2 block text-sm font-medium text-gray-700">
                      Workflow JSON
                    </label>
                    <textarea
                      placeholder={`Paste your ComfyUI LTX image-to-video workflow JSON here (export via "Export (API)" in ComfyUI)`}
                      className="w-full rounded-lg border border-gray-300 px-4 py-2.5 font-mono text-xs outline-none transition-colors focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20"
                      rows={6}
                      value={llmConfig.COMFYUI_MOTION_WORKFLOW || ""}
                      onChange={(event) =>
                        update("COMFYUI_MOTION_WORKFLOW", event.target.value)
                      }
                    />
                    <p className="mt-2 text-sm text-gray-500">
                      The image node that receives the slide image (the LTX
                      start frame) must be titled &quot;Load Image&quot;. Optional
                      nodes: a text node titled &quot;Input Prompt&quot; (motion
                      description) and int nodes titled &quot;Width&quot;,
                      &quot;Height&quot; (set from the image&apos;s aspect ratio)
                      and &quot;Duration&quot; (seconds).
                    </p>
                  </div>

                  <div className="rounded-lg border border-[#D9D6FE] bg-[#F4F3FF] p-3 text-xs text-[#5146E5]">
                    Clips are generated from an image&apos;s editor toolbar and
                    only play in exported videos. If generation fails, the
                    static image is used instead. Any audio LTX produces is
                    discarded. Image generation, narration and LTX share one
                    ComfyUI GPU — see VIDEO_MOTION_MAX_CONCURRENCY.
                  </div>
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
};

export default VideoMotionProvider;
