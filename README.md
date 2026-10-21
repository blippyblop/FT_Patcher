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

# What do the options mean

Patch Meta File - Hdiffs also come with a diff for patching the meta file with some bone-value tweaks. This applies those too. 

Force resample on custom mesh - Only applies in shape key transfer mode - if your mesh fails the validation checks you can just force it and see if it works. 

Mode:

Shape Key Transfer- This is 'best effort' trying to push the blendshapes onto your mesh. If you do this, check the shapekeys manually since if there was any edited mesh it'll break it.

Head Swap - The more robust option but it relies on a clean headswap being viable.

Dry run - This runs the validation code without actually patching the fbx files. useful if you're not sure if your head mesh is identical or not.

# Just give me the exe, nerd

It's in releases. Look for it on the right hand side panel thing.

# Changelog 

1.2 - Fixed FBX export scale for avatar files saved in meter units (e.g. the schuul variant): the output now matches the source file's unit scale. The exporter runs with a global scale derived from the source's UnitScaleFactor (1 = cm, 100 = m, anything else supported too), so every node transform lands on the source's own values, and the output file's unit declaration is re-tagged to the source's. Files in centimeter units export exactly as before.

1.1 - The "Force resample on custom mesh" checkbox now only appears when Shape Key Transfer is selected.

1.0 - First release. 
