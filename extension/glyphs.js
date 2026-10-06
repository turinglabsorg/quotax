import Cairo from 'cairo';
import Clutter from 'gi://Clutter';
import GObject from 'gi://GObject';
import St from 'gi://St';

const TAU = Math.PI * 2;

// Custom stroked shapes, line width 15% of the glyph size, round caps and joins (see DESIGN.md).
const SHAPES = {
    // Ten-ray burst with alternating ray length.
    claude(cr, size) {
        const center = size / 2;
        const radius = size / 2 * 0.92;
        for (let index = 0; index < 10; index++) {
            const angle = index * Math.PI / 5 - Math.PI / 2;
            const outer = radius * (index % 2 === 0 ? 1 : 0.78);
            const inner = radius * 0.2;
            cr.moveTo(center + Math.cos(angle) * inner, center + Math.sin(angle) * inner);
            cr.lineTo(center + Math.cos(angle) * outer, center + Math.sin(angle) * outer);
        }
    },
    // Terminal prompt `>_`.
    codex(cr, size) {
        cr.moveTo(size * 0.1, size * 0.2);
        cr.lineTo(size * 0.46, size * 0.5);
        cr.lineTo(size * 0.1, size * 0.8);
        cr.moveTo(size * 0.58, size * 0.82);
        cr.lineTo(size * 0.92, size * 0.82);
    },
    // Open ring with a diagonal slash.
    grok(cr, size) {
        const center = size / 2;
        cr.arc(center, center, size * 0.34, -10 / 360 * TAU, 280 / 360 * TAU);
        cr.moveTo(center - size * 0.44, center + size * 0.44);
        cr.lineTo(center + size * 0.44, center - size * 0.44);
    },
    // Llama head: two ears leaning outwards over a rounded head with two eyes.
    ollama(cr, size) {
        cr.moveTo(size * 0.37, size * 0.4);
        cr.lineTo(size * 0.29, size * 0.08);
        cr.moveTo(size * 0.63, size * 0.4);
        cr.lineTo(size * 0.71, size * 0.08);
        const [left, right, top, bottom, radius] = [size * 0.16, size * 0.84, size * 0.4, size * 0.94, size * 0.2];
        cr.newSubPath();
        cr.arc(right - radius, top + radius, radius, -TAU / 4, 0);
        cr.arc(right - radius, bottom - radius, radius, 0, TAU / 4);
        cr.arc(left + radius, bottom - radius, radius, TAU / 4, TAU / 2);
        cr.arc(left + radius, top + radius, radius, TAU / 2, TAU * 3 / 4);
        cr.closePath();
        // Round caps turn these zero-length strokes into dots.
        for (const x of [0.39, 0.61]) {
            cr.moveTo(size * x, size * 0.64);
            cr.lineTo(size * x, size * 0.64);
        }
    },
    // Gauge with the needle at one third, shown when nothing is linked yet.
    gauge(cr, size) {
        const center = size / 2;
        const radius = size * 0.42;
        cr.arc(center, center + size * 0.06, radius, 150 / 360 * TAU, 390 / 360 * TAU);
        const angle = 229 / 360 * TAU;
        cr.moveTo(center, center + size * 0.06);
        cr.lineTo(center + Math.cos(angle) * radius * 0.62, center + size * 0.06 + Math.sin(angle) * radius * 0.62);
    },
};

export const Glyph = GObject.registerClass(
class QuotaGlyph extends St.DrawingArea {
    _init(shape, styleClass) {
        super._init({style_class: `quota-glyph ${styleClass}`, y_align: Clutter.ActorAlign.CENTER});
        this._shape = shape;
    }

    vfunc_repaint() {
        const cr = this.get_context();
        const [width, height] = this.get_surface_size();
        const size = Math.min(width, height);
        cr.translate((width - size) / 2, (height - size) / 2);
        cr.setSourceColor(this.get_theme_node().get_foreground_color());
        cr.setLineWidth(Math.max(1.3, size * 0.15));
        cr.setLineCap(Cairo.LineCap.ROUND);
        cr.setLineJoin(Cairo.LineJoin.ROUND);
        SHAPES[this._shape]?.(cr, size);
        cr.stroke();
        cr.$dispose();
    }
});

// Capsule usage bar: the track uses the text color at low opacity, the fill `-quota-fill-color`.
export const UsageBar = GObject.registerClass(
class QuotaUsageBar extends St.DrawingArea {
    _init(fraction, level) {
        super._init({style_class: `quota-bar level-${level}`, x_expand: true});
        this._fraction = Math.min(1, Math.max(0, fraction));
    }

    vfunc_repaint() {
        const cr = this.get_context();
        const [width, height] = this.get_surface_size();
        const node = this.get_theme_node();
        const text = node.get_foreground_color();
        const radius = height / 2;

        const capsule = barWidth => {
            cr.newPath();
            cr.arc(radius, radius, radius, TAU / 4, TAU * 3 / 4);
            cr.arc(barWidth - radius, radius, radius, -TAU / 4, TAU / 4);
            cr.closePath();
        };

        capsule(width);
        cr.setSourceRGBA(text.red / 255, text.green / 255, text.blue / 255, 0.13);
        cr.fill();

        if (this._fraction > 0) {
            // Non-zero values stay visible: at least as wide as the bar is tall.
            capsule(Math.max(height, width * this._fraction));
            cr.setSourceColor(node.get_color('-quota-fill-color'));
            cr.fill();
        }
        cr.$dispose();
    }
});
