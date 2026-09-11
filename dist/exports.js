import {binarySTL,volume,zip} from './geometry.js';
export function scadSource(m,kernel){const p=m.p;return `// PETAL / Parametric Expeditionary Tactical Aperture Lab\n// Editable source. Dimensions in mm. No external libraries.\n/* [Dish] */\ndiameter = ${p.diameter}; // [180:1:1200]\nfd = ${p.fd}; // [0.25:0.01:0.80]\nthickness = ${p.thickness}; // [1.6:0.1:6]\n/* [Joints] */\njoint_style = ${p.jointStyle}; // [0:Legacy straps,1:Keyed saddles]\njoint_clearance = ${p.jointClearance}; // [0.1:0.05:0.4]\nadaptive_joints = ${p.adaptiveJoints}; // [0:Off,1:On]\nconnector_spacing = ${p.connectorSpacing}; // [60:5:180]\nstagger_rings = ${p.staggerRings}; // [0:Aligned,1:Half-petal stagger]\n/* [Rear construction] */\nrear_style = ${p.rearStyle}; // [0:Curved shell,1:Two-facet rear]\nfacet_angle = ${p.facetAngle}; // [10:1:15]\n/* [Printer] */\nbed_x = ${p.bedX};\nbed_y = ${p.bedY};\nbed_z = ${p.bedZ};\nmargin = ${p.margin};\n/* [Segmentation] */\n// 0 = automatic. Explicit values avoid repeating the search on each render.\nsectors = ${m.layout.n}; // [0:2:32]\nrows = ${m.layout.rows}; // [0:1:12]\ngap = ${p.gap}; // [0.2:0.1:1]\nresolution = ${p.resolution}; // [2:1:10]\n/* [Print orientation and breakaway ribs] */\nsupports = ${p.supports}; // [0:Off,1:On]\nprint_angle = ${p.printAngle}; // -1 auto; 0 low profile; or 45-70 degrees\nrib_count = ${p.ribCount}; // [2:1:5]\ncontact_gap = ${p.contactGap}; // [0.1:0.05:0.4]\ncontact_width = ${p.contactWidth}; // [0.4:0.1:0.8]\nrib_pitch = ${p.ribPitch}; // [6:1:20]\n/* [Export] */\npart = "assembly"; // [assembly,panel,side-bridge,ring-bridge,hub-rear,hub-clamp]\nring = 1; // One-based radial ring index\nstation = 1; // One-based saddle position in this ring\n\n${kernel}`;}
export function manifest(m){const p=m.p;return {generator:'PETAL 2.2',name:'Parametric Expeditionary Tactical Aperture Lab',units:'mm',parameters:p,segmentation:{petals:m.layout.n,rings:m.layout.rows,staggered:Boolean(p.jointStyle&&p.staggerRings),ring_offsets_degrees:m.ringPhases.map(a=>a*180/Math.PI),adaptive_connectors:Boolean(p.jointStyle&&p.adaptiveJoints),target_connector_spacing_mm:p.connectorSpacing},focal_length:m.focal,dish_depth:m.depth,interface:{revision:p.jointStyle?3:1,type:'custom',joint:p.jointStyle?'Keyed saddles: two bolts within rings, four between rings':'Legacy two-bolt straps',hub_outer_diameter:120,front_clamp_outer_diameter:p.jointStyle?116:120,center_opening:30,rear_mount_bolt_circle:p.jointStyle?60:40,rear_mount_holes:4,rear_mount_clocking_degrees:45,clearance_window:4.6,hardware:'M4 bolts, nuts and washers',...(p.jointStyle?{tongue_width:8,tongue_height:2.6,socket_width:8+2*p.jointClearance,socket_clearance_per_side:p.jointClearance,rear_pad_depth:3,socket_floor_below_original_rear:.4,axial_assembly_gap:.2}:{})},assembly_bolts:m.bolts,parts:m.parts.map(p=>({file:`${p.id}${p.supportMeshes.length?'_supported':''}_qty-${p.qty}.stl`,quantity:p.qty,scad_part:p.kind==='panel'?'panel':p.id.startsWith('side-bridge')?'side-bridge':p.id.startsWith('ring-bridge')?'ring-bridge':p.id,scad_ring:Math.max(1,p.row+1),scad_station:(p.spec.station||0)+1,assembly_rotations_degrees:m.instances.filter(i=>i.part.id===p.id).map(i=>i.a*180/Math.PI),vertical_shell_wall_range_mm:p.wallRange,grip_stack_range_mm:p.gripRange,petal_angle_degrees:p.angle,support_ribs:p.supportMeshes.length,support_volume_cm3:+(p.supportMeshes.reduce((s,r)=>s+volume(r),0)/1000).toFixed(3),bed_rotation_degrees:p.bedRotation,dimensions_mm:p.dim.map(x=>+x.toFixed(3)),solid_volume_cm3:+(volume(p.mesh)/1000).toFixed(3)})),notes:['Print a complete matching kit after geometry or interface changes. Revision 3 panels and side saddles require a matching kit; do not mix with earlier four-bolt side joints. Hub mount remains 60 mm BCD.','Locating tongues register the parts; bolts supply clamping. This is a clearance fit, not a snap fit or interference fit.','Solid CAD volume is not a filament estimate. Socket pads add material; strength has not been load-tested.','Keep supported STL shells together in their original positions. Check slicer toolpaths and tune contact gap.','Geometry checked digitally; physical fit, strength and printing remain unvalidated.']};}
export function guide(m){const p=m.p;return `# PETAL — assembly and print guide

${p.diameter} mm diameter · f/D ${p.fd} · ${m.depth.toFixed(2)} mm depth · ${m.focal.toFixed(2)} mm focus.
${m.layout.n} petals × ${m.layout.rows} concentric rings. All dimensions in millimetres.

## Connection system

${p.jointStyle?`Interface revision 3: rear-installed keyed saddles. Same-ring saddles have two M4 bolts and two 8 mm locating tongues, one per petal. Between-ring saddles retain four M4 bolts and four tongues, two per petal. The tongues enter ${8+2*p.jointClearance} mm rear sockets (${p.jointClearance} mm clearance per side). 12 mm docking pads add 3 mm behind the original shell. The socket floor stops 0.4 mm before reaching the original rear surface; nominal shell thickness is preserved outside the bolt bores. Tongues are 2.6 mm tall with 0.2 mm axial clearance in the assembled model.

The keys locate the parts and carry lateral bearing; the bolts provide clamping. No flexible latch or press fit is required. All fittings install from behind, so the last panel can be lowered into place without sliding along a continuous interlocking seam. This is discrete tongue-and-socket registration through the joint piece, not a continuous tongue-and-groove edge.

The hub uses one locating shoulder per petal root, a flat rear mounting face, and a 116 mm OD front clamp with a 94 mm opening. The custom mounting interface is FOUR M4 windows on a 60 mm bolt circle at 45 degrees, with a 30 mm center opening and 120 mm hub OD. It does NOT fit legacy 40 mm BCD adapters. Print the entire revised kit; do not mix joint generations.`:`Interface revision 1: curved two-bolt backing straps and a center sandwich clamp. Custom rear mount: four M4 windows on a 40 mm bolt circle at 45 degrees, 30 mm center opening and 120 mm hub OD. This is the legacy interface.`}

## Ring staggering and connector placement

${p.jointStyle&&p.staggerRings?`Alternate rings are rotated by half a petal (${(180/m.layout.n).toFixed(3)} degrees), so a petal overlaps two neighbors across each ring boundary. Connectors are centered within those overlaps; they do not straddle three-panel junctions.`:'Radial seams are aligned between rings.'}

Ring offsets from the innermost ring: ${m.ringPhases.map((a,j)=>`ring ${j+1}: ${(a*180/Math.PI).toFixed(3)} degrees`).join('; ')}.

${p.jointStyle&&p.adaptiveJoints?`Adaptive connector placement is ON, with a ${p.connectorSpacing} mm target spacing. Longer side seams receive extra two-bolt saddles; ring overlaps receive four-bolt saddles. The planner reserves space at ends and rejects crowded sockets. Spacing is a geometric construction rule; it is not derived from loads, material properties or wind.`:'Adaptive connector placement is OFF. Keyed joints use one saddle per side seam and one per neighboring-ring overlap.'}

Changing staggering, spacing, diameter or segmentation changes the hole pattern. Reprint matching panels and fittings. Saddle suffixes identify positions: side stations run from center toward rim; ring stations run counterclockwise within one inner petal. Identical curved-back ring saddles share a file and increased quantity; faceted backs have position-specific saddles. Refer to assembly rotations and SCAD station numbers in parameters.json. A seam gap remains intentional; do not close it by bending parts.

## Print list

${m.parts.map(p=>`- ${p.id}${p.supportMeshes.length?'_supported':''}_qty-${p.qty}.stl — print ${p.qty}; ${p.dim.map(x=>x.toFixed(1)).join(' × ')} mm; SCAD station ${(p.spec.station||0)+1}`).join('\n')}

## Rear construction

${p.rearStyle?`Two planar rear faces with a ${p.facetAngle} degree change of slope, plus local docking pads when keyed joints are selected. The front remains parabolic. Thickness sets a minimum vertical shell wall; corners may be much thicker.`:'Curved shell with constant vertical wall thickness, plus local rear docking pads when keyed joints are selected.'} Solid CAD volume assumes a fully solid body; slicer infill determines material use. No skeletonization is applied.

## Printing and first fit check

Start with one panel, its saddle and the center rings. Check the joint without forcing it. Increase socket clearance if tongues bind; use washers and tighten gently. Do not paint mating sockets or tongues before the fit check. Digital mesh checks do not verify physical stiffness, shrinkage, creep, clamp force or tolerances.

Petal orientations: ${m.parts.filter(p=>p.kind==='panel').map(p=>`ring ${p.row+1}: ${p.angle} degrees from bed, ${p.bedRotation} degrees bed rotation`).join('; ')}. Auto mode prefers 60 degrees when keyed joints, ribs or a faceted rear are selected, then tries 55, 50, 45 and 65 if needed. Bed rotations are checked in 15-degree steps. This is a geometric fit heuristic, not a strength or print-reliability simulation. Keyed saddles and the rear hub print on their flat bases with tongues upward. Their stepped top surfaces and panel socket ceilings still need slicer review.

Built-in ribs: ${p.supports?'ON':'OFF'}; ${p.ribCount} ribs per panel; nominal vertical gap ${p.contactGap} mm, ridge width ${p.contactWidth} mm, tooth spacing ${p.ribPitch} mm. Keyed-panel supports follow slices of the actual mesh, including docking pads. Ribs have an additional clearance allowance. Their feet start at Z=0 and the panel is lifted 5.6 mm. Preserve all shells as one object; do not independently auto-arrange or drop them to the bed. Inspect the layer preview before disabling automatic supports. Calibrate the gap with your material and layer height. Without built-in ribs, use slicer supports as needed.

## Hardware

${m.bolts} M4 assembly bolts and nuts, with washers on both faces. Four mount fasteners and an adapter are additional. Bolt windows are 4.6 mm polar rectangles, not printed threads.

${m.parts.some(p=>p.gripRange)?m.parts.filter(p=>p.gripRange).map(p=>`- ${p.name}: ${p.gripRange.map(x=>x.toFixed(1)).join('–')} mm grip stack`).join('\n'):`Legacy bridge grip is approximately ${(p.thickness+3.4).toFixed(1)} mm; hub grip approximately ${(p.thickness+9.6).toFixed(1)} mm.`}

Grip stacks exclude washers, nut thickness and thread engagement. Select actual bolt lengths using your hardware. No numerical strength improvement is claimed.

## Assembly

1. Orient panels with their parabolic faces forward. Seat inner roots on the matching rear hub; keyed roots register over its tongues.
2. Add the front clamp and root bolts loosely.
3. Bring neighboring panels to the designed seam gap. Install each matching saddle from behind. All locating tongues must seat without forcing before the bolts are tightened.
4. Add outer rings from the center outward using the listed rotation offsets. Install every matching side and ring saddle; staggered rings connect each inner petal to both adjacent outer petals.
5. Tighten in alternating steps with washers supporting the plastic. Check circularity and depth as the 0.2 mm axial CAD clearance is taken up. Do not use bolt torque to force misaligned sockets into position.
6. Attach the mount using the correct bolt circle for this interface generation.

## Limits and editable source

Panels describe z = r²/(4f), with f = diameter × f/D. Conductive finish, feed, feed support and electronics are separate. Openings, fasteners and seams interrupt the front surface. No RF performance, wind rating or structural load rating is provided. Large dishes may need additional bracing.

Open petal.scad, select a part and one-based ring, F6 Render, then export STL. The assembly view is for inspection, not a one-piece print. Set sectors and rows to zero to rerun segmentation after changing dimensions. Keyed layouts also reserve space between neighboring sockets. Geometry and assembly instructions must be regenerated together.

## Design references

- Connector choices and fit tolerance: https://help.prusa3d.com/article/cut-tool_1779
- Printed snap-fit design considerations: https://formlabs.com/global/blog/designing-3d-printed-snap-fit-enclosures/
- Support gap guidance: https://help.prusa3d.com/article/support-material_1698
- OpenSCAD syntax: https://openscad.org/cheatsheet/
`;}
export function kit(m,kernel){return zip([...m.parts.map(p=>({name:`STL/${p.id}${p.supportMeshes.length?'_supported':''}_qty-${p.qty}.stl`,data:binarySTL(p.output)})),{name:'petal.scad',data:scadSource(m,kernel)},{name:'parameters.json',data:JSON.stringify(manifest(m),null,2)},{name:'ASSEMBLY.md',data:guide(m)}]);}
