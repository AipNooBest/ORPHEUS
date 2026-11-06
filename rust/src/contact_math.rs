//! Float32 vector and rigid-frame math from the build 104319 contact solver.
//!
//! The operation order follows `ow174/game/contacts.py`. Keep intermediate
//! products and sums separate so they round in the same places as SSE.

pub type Vec3 = [f32; 3];

pub const TINY: f32 = f32::from_bits(0x057a_0000);

pub fn add(a: Vec3, b: Vec3) -> Vec3 {
    [a[0] + b[0], a[1] + b[1], a[2] + b[2]]
}

pub fn sub(a: Vec3, b: Vec3) -> Vec3 {
    [a[0] - b[0], a[1] - b[1], a[2] - b[2]]
}

pub fn mul(a: Vec3, b: Vec3) -> Vec3 {
    [a[0] * b[0], a[1] * b[1], a[2] * b[2]]
}

pub fn scale(a: Vec3, scalar: f32) -> Vec3 {
    a.map(|lane| lane * scalar)
}

pub fn dot(a: Vec3, b: Vec3) -> f32 {
    let [x, y, z] = mul(a, b);
    (x + y) + z
}

pub fn cross([ax, ay, az]: Vec3, [bx, by, bz]: Vec3) -> Vec3 {
    [ay * bz - az * by, az * bx - ax * bz, ax * by - ay * bx]
}

/// SSE max selects the second operand on equality or NaN.
pub fn maxps(a: f32, b: f32) -> f32 {
    if a > b { a } else { b }
}

/// SSE min selects the second operand on equality or NaN.
pub fn minps(a: f32, b: f32) -> f32 {
    if a < b { a } else { b }
}

/// Deterministic estimate followed by the reference's float32 Newton step.
pub fn rsqrt(value: f32) -> f32 {
    let estimate = if value == 0.0 {
        f32::INFINITY
    } else {
        (1.0 / f64::from(value).sqrt()) as f32
    };
    let half = (value * 0.5) * (estimate * estimate);
    (0.5 - half) * estimate + estimate
}

pub fn normalize(vector: Vec3) -> Vec3 {
    scale(vector, rsqrt(dot(vector, vector)))
}

pub fn rotate([x, y, z, w]: [f32; 4], vector: Vec3) -> Vec3 {
    let xyz = [x, y, z];
    let t = add(scale(vector, w), cross(xyz, vector));
    let c = cross(xyz, t);
    add(add(c, c), vector)
}

pub fn unrotate([x, y, z, w]: [f32; 4], vector: Vec3) -> Vec3 {
    let xyz = [x, y, z];
    let t = add(scale(vector, w), cross(vector, xyz));
    let c = cross(t, xyz);
    add(add(c, c), vector)
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct ContactFrame {
    pub translation: Vec3,
    /// Quaternion components in XYZW order.
    pub rotation: [f32; 4],
}

impl ContactFrame {
    pub const IDENTITY: Self = Self {
        translation: [0.0; 3],
        rotation: [0.0, 0.0, 0.0, 1.0],
    };

    pub fn place(self, local: Vec3) -> Vec3 {
        add(rotate(self.rotation, local), self.translation)
    }

    pub fn local(self, world: Vec3) -> Vec3 {
        unrotate(self.rotation, sub(world, self.translation))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn vector_rounding_and_sse_operand_selection_match_python() {
        let a = [2048.0, 0.25, -2048.0];
        let b = [8192.0, 1.25, 8192.0];
        assert_eq!(dot(a, b).to_bits(), 0);
        assert_eq!(maxps(0.0, -0.0).to_bits(), (-0.0_f32).to_bits());
        assert_eq!(minps(f32::NAN, 1.0), 1.0);
        assert!(minps(1.0, f32::NAN).is_nan());
        assert_eq!(
            [0.3, 0.6, 2.1, 5.6, 120_000_001.0].map(|value| rsqrt(value).to_bits()),
            [
                0x3fe9_b1e8,
                0x3fa5_3f4e,
                0x3f30_a823,
                0x3ed8_5c07,
                0x38bf_7162
            ],
        );
    }

    #[test]
    fn quaternion_frames_match_python_operation_order() {
        let quarter = std::f32::consts::FRAC_1_SQRT_2;
        let frame = ContactFrame {
            translation: [1.15, -2.575, 0.125],
            rotation: [0.0, quarter, 0.0, quarter],
        };
        let point = [2.25, -3.125, 4.375];
        assert_eq!(
            rotate(frame.rotation, point).map(f32::to_bits),
            [0x408c_0000, 0xc048_0000, 0xc00f_fffe],
        );
        assert_eq!(
            unrotate(frame.rotation, point).map(f32::to_bits),
            [0xc08b_ffff, 0xc048_0000, 0x4010_0000],
        );
        assert_eq!(
            frame.place(point).map(f32::to_bits),
            [0x40b0_cccd, 0xc0b6_6666, 0xc007_fffe],
        );
    }
}
