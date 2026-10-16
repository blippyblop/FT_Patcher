# Face Tracking Patcher for Hash-style Face tracking

This tool takes a hash style face tracking addon and you can apply it to any FBX. It works outside of unity.

It takes your hash-style face tracking addon's "hdiff" file, and the FBX it's supposed to apply to, and then does a read of what the patch actually changed in the vanilla FBX, and then best effort patches your custom FBX with the same changes.

This is for situations when:

* You have a modified FBX but the head mesh is actually exactly the same
* You have a great many modified FBX of the above type and you are facing the prospect of doing a manual blender headswap for every single one

# Usage:

Just run the GUI. Put in

* Your hdiff file
* Your vanilla FBX
* Any FBXes you want to modify

Select if you want 'best effort' or a headswap (headswap is more reliable but is an entire headswap)
Make sure the .meta and meta diffs are included since there's meta-file tuning. It's not mandatory but it just chekcks the same folder. 
