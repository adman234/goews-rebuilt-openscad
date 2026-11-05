import asyncio
import io
import logging
import re
import zipfile
from enum import StrEnum
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, Field, field_validator
from sanic import response
from sanic.request import Request
from sanic_ext import openapi, validate
from server.api import api_bp
from server.enums import Variant
from server.openscad import build, top_dir
from server.step import build_separator_step, step_available
from server.threemf import Part, build_3mf

logger = logging.getLogger("tile_stack")


def _read_tile_thickness() -> float:
    """Tile thickness from constants.scad, so separator placement stays in sync."""
    text = (Path(top_dir) / "constants.scad").read_text()
    match = re.search(r"^\s*tile_thickness\s*=\s*([\d.]+)\s*;", text, re.M)
    return float(match.group(1)) if match else 6.0


TILE_THICKNESS = _read_tile_thickness()


@openapi.component
class TileKind(StrEnum):
    HEX = "hex"
    GRID = "grid"


@openapi.component
class StackPart(StrEnum):
    PLA = "pla"
    PETG = "petg"
    ALL = "all"


@openapi.component
class TileStackDefinition(BaseModel):
    tile_kind: TileKind = TileKind.HEX
    part: StackPart = StackPart.PLA

    stack_count: Annotated[
        int, Field(gt=1, description="Number of tiles in the stack")
    ] = 3
    spacer_h: Annotated[
        float, Field(gt=0, description="PETG separator thickness in mm")
    ] = 0.2
    spacer_xy_delta: Annotated[
        float, Field(description="XY shrink/expand for separator")
    ] = -0.2

    enable_pull_tabs: bool = True
    tab_side: Annotated[str, Field(description="right, left, front, or back")] = "right"
    tab_len: Annotated[float, Field(gt=0)] = 22
    tab_support_tile_gap: Annotated[float, Field(ge=0)] = 1.5

    columns: Annotated[int, Field(gt=0, description="Tile columns in units")] = 4
    rows: Annotated[int, Field(gt=0, description="Tile rows in units")] = 4

    fill_top: bool = False
    fill_bottom: bool = False
    fill_left: bool = False
    fill_right: bool = False
    reverse_stagger: bool = False
    exact_width: bool = False

    mounting_hole_shank_diameter: Annotated[float, Field(gt=0)] = 4
    mounting_hole_head_diameter: Annotated[float, Field(gt=0)] = 8
    mounting_hole_inset_depth: Annotated[float, Field(gt=0)] = 1
    mounting_hole_countersink_depth: Annotated[float, Field(ge=0)] = 2

    skip_list: Annotated[list[list[int]], Field(description="Tiles to exclude")] = []

    variant: Variant = Variant.ORIGINAL

    include_step: Annotated[
        bool,
        Field(
            description=(
                "Add a STEP file of the PETG separators to the ZIP download. "
                "This makes the download noticeably slower and larger."
            )
        ),
    ] = False

    @field_validator("skip_list", mode="after")
    @classmethod
    def validate_skip_list(cls, value):
        for coordinate in value:
            if len(coordinate) != 2:
                raise ValueError("Skip list must be a list of [row, column] pairs")
            if coordinate[0] <= 0 or coordinate[1] <= 0:
                raise ValueError(
                    "Skip list rows and columns must be greater than zero."
                )
        return value


def make_tile_stack_filename(body: TileStackDefinition) -> str:
    variant = "original" if body.variant.to_int() == 0 else "thicker_cleats"
    return (
        f"tile-stack-{body.tile_kind}-{body.part}-"
        f"{body.columns}x{body.rows}-"
        f"{body.stack_count}high-{variant}.stl"
    )


def tile_stack_build_params(body: TileStackDefinition, part: StackPart) -> dict:
    skip_list = ",".join(repr(entry) for entry in body.skip_list)

    return {
        "tile_kind": body.tile_kind.value,
        "part": part.value,
        "stack_count": body.stack_count,
        "spacer_h": body.spacer_h,
        "spacer_xy_delta": body.spacer_xy_delta,
        "enable_pull_tabs": body.enable_pull_tabs,
        "tab_side": body.tab_side,
        "tab_len": body.tab_len,
        "tab_support_tile_gap": body.tab_support_tile_gap,
        "variant": body.variant.to_int(),
        "columns": body.columns,
        "rows": body.rows,
        "fill_top": body.fill_top,
        "fill_bottom": body.fill_bottom,
        "fill_left": body.fill_left,
        "fill_right": body.fill_right,
        "reverse_stagger": body.reverse_stagger,
        "exact_width": body.exact_width,
        "mounting_hole_shank_diameter": body.mounting_hole_shank_diameter,
        "mounting_hole_head_diameter": body.mounting_hole_head_diameter,
        "mounting_hole_inset_depth": body.mounting_hole_inset_depth,
        "mounting_hole_countersink_depth": body.mounting_hole_countersink_depth,
        "skip_list": skip_list,
    }


def make_tile_stack_basename(body: TileStackDefinition) -> str:
    variant = "original" if body.variant.to_int() == 0 else "thicker-cleats"
    return (
        f"tile-stack-{body.tile_kind.value}-"
        f"{body.columns}x{body.rows}-"
        f"{body.stack_count}high-{variant}"
    )


@api_bp.post("/tile-stack")
@openapi.body(TileStackDefinition)
@openapi.response(200, "model/stl")
@openapi.summary("Tile Stack")
@openapi.description("Create a stacked GOEWS tile assembly for PLA/PETG printing")
@validate(json=TileStackDefinition)
async def tile_stack(request: Request, body: TileStackDefinition):
    filename = make_tile_stack_filename(body)

    return response.raw(
        await build(
            "tile_stack.scad",
            **tile_stack_build_params(body, body.part),
        ),
        content_type="model/stl",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@api_bp.post("/tile-stack-bundle")
@openapi.body(TileStackDefinition)
@openapi.response(200, "application/zip")
@openapi.summary("Tile Stack Bundle")
@openapi.description(
    "Create PLA tile and PETG spacer STLs for a stacked GOEWS tile assembly"
)
@validate(json=TileStackDefinition)
async def tile_stack_bundle(request: Request, body: TileStackDefinition):
    basename = make_tile_stack_basename(body)

    pla_task = build(
        "tile_stack.scad",
        **tile_stack_build_params(body, StackPart.PLA),
    )
    petg_task = build(
        "tile_stack.scad",
        **tile_stack_build_params(body, StackPart.PETG),
    )

    # 2D outline of one separator, used to build the STEP file.
    outline_task = build(
        "tile_stack.scad",
        export_format="svg",
        **{**tile_stack_build_params(body, StackPart.PETG), "part": "petg_outline"},
    )

    pla_stl, petg_stl, outline_svg = await asyncio.gather(
        pla_task, petg_task, outline_task
    )

    threemf = await asyncio.to_thread(
        build_3mf,
        basename,
        [
            Part("PLA tiles", pla_stl, extruder=1),
            Part("PETG separators", petg_stl, extruder=2),
        ],
    )

    step = None
    if body.include_step and not step_available():
        logger.warning("STEP requested but cadquery-ocp is not installed")
    elif body.include_step:
        try:
            step = await asyncio.to_thread(
                build_separator_step,
                outline_svg.decode(),
                thickness=body.spacer_h,
                first_z=TILE_THICKNESS,
                pitch=TILE_THICKNESS + body.spacer_h,
                count=body.stack_count - 1,
            )
        except Exception:
            logger.exception("Separator STEP export failed")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{basename}.3mf", threemf)
        zf.writestr(f"{basename}.stl", pla_stl)
        zf.writestr(f"{basename}-spacers.stl", petg_stl)
        if step is not None:
            zf.writestr(f"{basename}-spacers.step", step)

    return response.raw(
        buf.getvalue(),
        content_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{basename}.zip"',
        },
    )
